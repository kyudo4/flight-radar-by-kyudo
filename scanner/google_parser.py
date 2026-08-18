"""Parser for the current Google Flights server payload.

Google changed the rendered HTML used by fast-flights 2.x. The useful flight
cards are now stored in the ``script.ds:1`` JSON payload, so parsing that
payload is both faster and less fragile than pretending an empty page means
"no flights".
"""

import json
from collections import deque
from datetime import datetime

from selectolax.lexbor import LexborHTMLParser


class GoogleParseError(RuntimeError):
    """The source returned HTML that cannot be interpreted as flight data."""


class GoogleNoFlights(GoogleParseError):
    """Google explicitly returned no flight groups."""


def _time(value):
    if not isinstance(value, (list, tuple)) or not value:
        raise GoogleParseError("Brak godziny odlotu/przylotu")
    return int(value[0]), int(value[1]) if len(value) > 1 else 0


def _date(value):
    if not isinstance(value, (list, tuple)) or len(value) < 3:
        raise GoogleParseError("Brak daty odlotu/przylotu")
    return int(value[0]), int(value[1]), int(value[2])


def _datetime(date_value, time_value):
    return datetime(*_date(date_value), *_time(time_value))


def _script_text(html):
    parser = LexborHTMLParser(html)
    for script in parser.css("script"):
        # Google may append rollout/experiment classes to the data script.
        # Treat ``ds:1`` as a class token instead of requiring an exact
        # attribute value, otherwise a harmless extra class hides all fares.
        classes = (script.attributes.get("class") or "").split()
        if "ds:1" in classes:
            return script.text()
    raise GoogleParseError("Google nie zwrócił danych ds:1")


def _segment_details(segments):
    """Return duration/stops for one contiguous itinerary leg."""
    if not segments:
        raise GoogleParseError("Brak odcinków lotu")
    first = segments[0]
    last = segments[-1]
    departure_dt = _datetime(first[20], first[8])
    arrival_dt = _datetime(last[21], last[10])
    duration_h = round((arrival_dt - departure_dt).total_seconds() / 3600, 1)
    if duration_h <= 0:
        raise GoogleParseError("Nieprawidłowy czas odcinka")
    aircraft = []
    for segment in segments:
        plane = segment[17] if len(segment) > 17 else ""
        if plane and plane not in aircraft:
            aircraft.append(str(plane))
    return {
        "duration_h": duration_h,
        "stops": max(0, len(segments) - 1),
        "departure": "%02d:%02d – %02d:%02d" % (departure_dt.hour, departure_dt.minute, arrival_dt.hour, arrival_dt.minute),
        "aircraft": " / ".join(aircraft),
    }


def _split_round_trip(segments, origin, destination, return_date):
    """Split combined Google segments at the first confirmed return leg."""
    if not (return_date and origin and destination):
        return segments, [], False
    expected_date = tuple(int(part) for part in str(return_date).split("-"))
    for index, segment in enumerate(segments):
        segment_origin = segment[3] if len(segment) > 3 else ""
        segment_date = segment[20] if len(segment) > 20 else None
        if (index == 0
                or str(segment_origin).upper() != str(destination).upper()
                or segment_date is None
                or _date(segment_date) != expected_date):
            continue

        outbound = segments[:index]
        inbound = segments[index:]
        outbound_origin = outbound[0][3] if len(outbound[0]) > 3 else ""
        outbound_destination = outbound[-1][6] if len(outbound[-1]) > 6 else ""
        inbound_destination = inbound[-1][6] if len(inbound[-1]) > 6 else ""
        if (str(outbound_origin).upper() == str(origin).upper()
                and str(outbound_destination).upper() == str(destination).upper()
                and str(inbound_destination).upper() == str(origin).upper()):
            return outbound, inbound, True
    return segments, [], False


def _looks_like_group(value):
    """Recognize a flight-group list without relying on one payload index."""
    if not isinstance(value, list) or not value:
        return False
    checked = 0
    valid = 0
    for row in value[:12]:
        checked += 1
        if not isinstance(row, list) or len(row) < 2:
            continue
        flight = row[0]
        price = row[1]
        if (isinstance(flight, list) and len(flight) > 2
                and isinstance(flight[2], list)
                and isinstance(price, list)):
            valid += 1
    return valid > 0 and valid >= max(1, checked // 3)


def _find_groups(payload):
    """Find flight groups across Google payload layout revisions.

    The server response has moved the useful list several times. The legacy
    path remains preferred, while bounded recursive discovery supports a
    moved list and still rejects a genuinely malformed response.
    """
    if not isinstance(payload, list):
        raise GoogleParseError("Nieprawidłowa struktura danych lotów Google")

    if len(payload) > 3 and payload[3] is not None:
        legacy = payload[3]
        if isinstance(legacy, list) and legacy and _looks_like_group(legacy[0]):
            return legacy[0]

    queue = deque([(payload, 0)])
    visited = 0
    while queue and visited < 10000:
        value, depth = queue.popleft()
        visited += 1
        if depth > 8 or not isinstance(value, list):
            continue
        if _looks_like_group(value):
            return value
        for child in value[:80]:
            if isinstance(child, list):
                queue.append((child, depth + 1))
    # An empty legacy slot is not proof that a route has no flights. Google
    # regularly leaves that placeholder empty while moving results elsewhere.
    # Keep this as a parse error so the caller verifies the page using the
    # rendered-browser fallback instead of silently returning zero offers.
    raise GoogleParseError("Nieprawidłowa struktura danych lotów Google")


def parse(html, origin=None, destination=None, return_date=None):
    script = _script_text(html)
    try:
        data = script.split("data:", 1)[1].rsplit(",", 1)[0]
        payload = json.loads(data)
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GoogleParseError("Nie można odczytać danych lotów Google") from exc

    groups = _find_groups(payload)

    flights = []
    for row in groups:
        try:
            flight = row[0]
            segments = flight[2]
            airlines = [str(value) for value in (flight[1] or []) if value]
            price = row[1][0][1]
            if not segments or price in (None, ""):
                continue
            outbound_segments, return_segments, return_verified = _split_round_trip(
                segments, origin, destination, return_date
            )
            outbound = _segment_details(outbound_segments)
            inbound = _segment_details(return_segments) if return_verified else None
            duration_h = max(outbound["duration_h"], inbound["duration_h"]) if inbound else outbound["duration_h"]
            stops = max(outbound["stops"], inbound["stops"]) if inbound else outbound["stops"]
            aircraft = [value for value in (outbound["aircraft"], inbound["aircraft"] if inbound else "") if value]
            flights.append({
                "airline_name": " / ".join(airlines),
                "price_pln": price,
                "duration_h": duration_h,
                "stops": stops,
                "departure": outbound["departure"],
                "aircraft": " / ".join(dict.fromkeys(aircraft)),
                "segments": len(segments),
                "round_trip_verified": return_verified,
                "outbound_duration_h": outbound["duration_h"],
                "outbound_stops": outbound["stops"],
                "return_duration_h": inbound["duration_h"] if inbound else None,
                "return_stops": inbound["stops"] if inbound else None,
                "return_departure": inbound["departure"] if inbound else "",
            })
        except (IndexError, KeyError, TypeError, ValueError, OverflowError):
            continue
    if not flights:
        raise GoogleParseError("Google zwrócił grupy bez czytelnych ofert")
    return flights
