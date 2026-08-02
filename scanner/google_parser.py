"""Parser for the current Google Flights server payload.

Google changed the rendered HTML used by fast-flights 2.x. The useful flight
cards are now stored in the ``script.ds:1`` JSON payload, so parsing that
payload is both faster and less fragile than pretending an empty page means
"no flights".
"""

import json
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
        if (script.attributes.get("class") or "") == "ds:1":
            return script.text()
    raise GoogleParseError("Google nie zwrócił danych ds:1")


def parse(html):
    script = _script_text(html)
    try:
        data = script.split("data:", 1)[1].rsplit(",", 1)[0]
        payload = json.loads(data)
    except (IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise GoogleParseError("Nie można odczytać danych lotów Google") from exc

    try:
        groups = payload[3][0]
    except (IndexError, TypeError) as exc:
        raise GoogleParseError("Nieprawidłowa struktura danych lotów Google") from exc
    if not groups:
        raise GoogleNoFlights("Google nie znalazł lotów")

    flights = []
    for row in groups:
        try:
            flight = row[0]
            segments = flight[2]
            airlines = [str(value) for value in (flight[1] or []) if value]
            price = row[1][0][1]
            if not segments or price in (None, ""):
                continue
            first = segments[0]
            last = segments[-1]
            departure_dt = _datetime(first[20], first[8])
            arrival_dt = _datetime(last[21], last[10])
            duration_h = round((arrival_dt - departure_dt).total_seconds() / 3600, 1)
            if duration_h <= 0:
                continue
            aircraft = []
            for segment in segments:
                plane = segment[17] if len(segment) > 17 else ""
                if plane and plane not in aircraft:
                    aircraft.append(str(plane))
            flights.append({
                "airline_name": " / ".join(airlines),
                "price_pln": price,
                "duration_h": duration_h,
                "stops": max(0, len(segments) - 1),
                "departure": "%02d:%02d – %02d:%02d" % (departure_dt.hour, departure_dt.minute, arrival_dt.hour, arrival_dt.minute),
                "aircraft": " / ".join(aircraft),
                "segments": len(segments),
            })
        except (IndexError, KeyError, TypeError, ValueError, OverflowError):
            continue
    if not flights:
        raise GoogleParseError("Google zwrócił grupy bez czytelnych ofert")
    return flights
