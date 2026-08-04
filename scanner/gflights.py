# -*- coding: utf-8 -*-
"""
Źródło Google Flights — nieoficjalne, bez przeglądarki.

Buduje parametr tfs (protobuf) przez fast-flights, pobiera stronę z cookie
CONSENT=YES+cb (omija unijną ścianę zgody — Google renderuje wtedy wyniki
po stronie serwera) i parsuje serwerowy payload ds:1 własnym parserem.
"""

import re
import os
import urllib.error
import urllib.parse
import urllib.request

from fast_flights import FlightData, Passengers
from fast_flights.filter import create_filter

import google_parser
import google_browser

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")

# Aliasy nazw linii z Google Flights -> kod IATA (uzupełnia priority_airlines)
AIRLINE_ALIASES = {
    "qatar airways": "QR", "qatar": "QR", "etihad": "EY", "emirates": "EK",
    "oman air": "WY", "turkish airlines": "TK", "turkish": "TK",
    "eva air": "BR", "eva": "BR", "singapore airlines": "SQ",
    "cathay pacific": "CX", "ana": "NH", "all nippon": "NH",
    "japan airlines": "JL", "jal": "JL", "lot": "LO", "lufthansa": "LH",
    "austrian": "OS", "swiss": "LX", "finnair": "AY", "klm": "KL",
    "air france": "AF", "china airlines": "CI", "korean air": "KE",
    "asiana": "OZ", "thai": "TG", "vietnam airlines": "VN",
    "malaysia airlines": "MH", "air china": "CA", "china eastern": "MU",
    "china southern": "CZ", "saudia": "SV", "gulf air": "GF",
    "kuwait airways": "KU", "egyptair": "MS", "etihad airways": "EY",
}


class BlockedError(Exception):
    """Google pokazał consent wall / blokadę zamiast wyników."""


class SourceParseError(RuntimeError):
    """Google odpowiedział, ale format nie zawierał czytelnych danych."""


class SourceCapacityError(RuntimeError):
    """Bezpieczny limit cięższego, renderowanego odczytu został wykorzystany."""


def airline_code(name):
    low = (name or "").lower()
    for alias, code in AIRLINE_ALIASES.items():
        if alias in low:
            return code
    return ""


def build_url(origin, dest, date, seat="business", return_date=None):
    legs = [FlightData(date=date, from_airport=origin, to_airport=dest)]
    trip = "one-way"
    if return_date:
        legs.append(FlightData(date=return_date, from_airport=dest, to_airport=origin))
        trip = "round-trip"
    filt = create_filter(
        flight_data=legs, trip=trip, seat=seat, passengers=Passengers(adults=1))
    q = urllib.parse.urlencode({
        "tfs": filt.as_b64().decode(), "hl": "en",
        "curr": "PLN", "tfu": "EgQIABABIgA"})
    return "https://www.google.com/travel/flights?" + q


def _parse_price_pln(s):
    m = re.search(r"([\d][\d\s,.]*)", (s or "").replace("PLN", ""))
    if not m:
        return None
    raw = re.sub(r"\s+", "", m.group(1))
    try:
        if "," in raw and "." in raw:
            decimal = "." if raw.rfind(".") > raw.rfind(",") else ","
            thousands = "," if decimal == "." else "."
            raw = raw.replace(thousands, "").replace(decimal, ".")
        elif "," in raw:
            parts = raw.split(",")
            raw = "".join(parts) if len(parts[-1]) == 3 else ".".join(parts)
        elif "." in raw:
            parts = raw.split(".")
            if len(parts[-1]) == 3 and all(part.isdigit() for part in parts):
                raw = "".join(parts)
        return int(round(float(raw)))
    except ValueError:
        return None


def _fetch_server(origin, dest, date, seat="business", return_date=None, timeout=35):
    """Lekki odczyt z HTML serwera, bez uruchamiania przeglądarki."""
    url = build_url(origin, dest, date, seat, return_date=return_date)
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,application/xhtml+xml")
    req.add_header("Accept-Language", "en-US,en;q=0.9")
    req.add_header("Cookie", "CONSENT=YES+cb")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", "ignore")
            status = resp.status
    except urllib.error.HTTPError as exc:
        # 409/403/429/503 są traktowane jako blokada lub throttling Google.
        # Nie ponawiamy ich w tym samym przebiegu, żeby nie pogłębiać blokady.
        if exc.code in {409, 403, 429, 503}:
            raise BlockedError("Google HTTP %s" % exc.code) from exc
        raise
    # Nie szukamy markerów blokady w surowym JS: normalna strona Google ma
    # ścieżki reCAPTCHA w kodzie, mimo że użytkownik nie widzi CAPTCHA.
    visible_head = re.sub(r"<script\b[^>]*>.*?</script\s*>", " ", body[:800000], flags=re.I | re.S)
    visible_head = re.sub(r"<style\b[^>]*>.*?</style\s*>", " ", visible_head, flags=re.I | re.S).lower()
    block_markers = ("before you continue", "captcha", "unusual traffic", "not a robot", "automated queries")
    if status != 200 or any(marker in visible_head for marker in block_markers):
        raise BlockedError("Google consent/CAPTCHA wall / status %s" % status)
    try:
        parsed = google_parser.parse(body, origin=origin, destination=dest, return_date=return_date)
    except google_parser.GoogleNoFlights:
        return None, []
    except google_parser.GoogleParseError as exc:
        # Nie oznaczamy zmienionego HTML jako "brak lotów". Taki przebieg musi
        # trafić do statusu partial i zatrzymać automatyczne zwiększanie limitu.
        raise SourceParseError(str(exc)) from exc
    flights = []
    for fl in parsed:
        price = _parse_price_pln(str(fl.get("price_pln", "")))
        if not price:
            continue
        stops = fl.get("stops")
        duration_h = fl.get("duration_h")
        if duration_h is None or stops is None:
            continue
        airline_name = fl.get("airline_name", "")
        flights.append({
            "airline_name": airline_name,
            "airline": airline_code(airline_name),
            "price_pln": price,
            "duration_h": duration_h,
            "stops": stops,
            "departure": fl.get("departure", ""),
            "aircraft": fl.get("aircraft", ""),
            "link": url,
            "round_trip_verified": bool(fl.get("round_trip_verified", not bool(return_date))),
            # A lightweight response is only a search result. It does not
            # prove that this exact itinerary reaches a booking/payment page.
            # Only the rendered itinerary picker may set this to True.
            "purchase_link_verified": bool(fl.get("purchase_link_verified", False)),
            "outbound_duration_h": fl.get("outbound_duration_h"),
            "outbound_stops": fl.get("outbound_stops"),
            "return_duration_h": fl.get("return_duration_h"),
            "return_stops": fl.get("return_stops"),
            "return_departure": fl.get("return_departure", ""),
        })
    if not flights:
        raise SourceParseError("Google zwrócił oferty bez wymaganej ceny/czasu/przesiadek")
    return parsed[0].get("price_pln") if parsed else None, flights


def fetch_gf(origin, dest, date, seat="business", return_date=None, timeout=35):
    """Return live offers, falling back to one reusable rendered Chrome page."""
    url = build_url(origin, dest, date, seat, return_date=return_date)
    try:
        return _fetch_server(origin, dest, date, seat, return_date, timeout)
    except (BlockedError, SourceParseError) as lightweight_error:
        if os.environ.get("GOOGLE_BROWSER_FALLBACK", "true").lower() == "false":
            raise
        try:
            rendered = google_browser.fetch_rendered(url, return_date=return_date)
        except google_browser.BrowserCapacityError as exc:
            raise SourceCapacityError(str(exc)) from exc
        except google_browser.BrowserBlockedError as exc:
            raise BlockedError(str(exc)) from exc
        except google_browser.BrowserNoFlightsError:
            return None, []
        except google_browser.BrowserParseError as exc:
            raise SourceParseError(
                "%s; odczyt renderowany: %s" % (str(lightweight_error), str(exc))
            ) from exc
        flights = []
        for flight in rendered:
            normalized = dict(flight)
            normalized["airline"] = airline_code(normalized.get("airline_name", ""))
            normalized["link"] = normalized.get("link") or url
            # Never infer payment verification from a generic Google search
            # URL. Rendered results must explicitly carry the flag.
            normalized["purchase_link_verified"] = bool(normalized.get("purchase_link_verified", False))
            flights.append(normalized)
        if not flights:
            raise SourceParseError("Awaryjny Chrome nie zwrócił ofert")
        return None, flights


def verify_purchase_links(origin, dest, date, seat="business", return_date=None):
    """Resolve exact booking links for a single queued Google query.

    The server payload is intentionally used for the cheap first pass. This
    bounded rendered pass is called only for candidates that can otherwise
    become Telegram alerts, and returns only cards selected through Google's
    itinerary picker.
    """
    url = build_url(origin, dest, date, seat, return_date=return_date)
    return google_browser.fetch_rendered(url, return_date=return_date)


def _best_value(flights):
    """Najlepsza oferta = najtańsza, a przy zbliżonej cenie (do +2%) NAJKRÓTSZA.
    Bez tego łapaliśmy przypadkową trasę z kilku o tej samej cenie — Google
    Flights domyślnie pokazuje najkrótszą, i tak samo musimy robić my."""
    minp = min(f["price_pln"] for f in flights)
    window = max(minp * 1.02, minp + 60)
    near = [f for f in flights if f["price_pln"] <= window]
    return min(near, key=lambda f: (f["duration_h"] if f["duration_h"] else 999,
                                    f["stops"] if f["stops"] is not None else 9,
                                    f["price_pln"]))


def cheapest_picks(flights, priority_codes, max_options=3):
    """Najlepsze warianty różnych przewoźników.

    Google Flights potrafi pokazać tańszą linię po poprzednim skanie. Dlatego
    nie zapisujemy już tylko jednej oferty ogółem i jednej priorytetowej:
    dla każdej linii wybieramy najrozsądniejszy wariant, zapisujemy kilka
    najtańszych linii oraz dodatkowo linię priorytetową, jeśli jej brakuje.
    """
    if not flights:
        return []
    by_airline = {}
    for flight in flights:
        key = flight["airline_name"] or flight["airline"] or "unknown"
        by_airline.setdefault(key, []).append(flight)
    candidates = [_best_value(group) for group in by_airline.values()]
    candidates.sort(key=lambda f: (f["price_pln"], f["duration_h"] or 999,
                                   f["stops"] if f["stops"] is not None else 9))
    priorities = [f for f in candidates if f["airline"] in priority_codes]
    non_priorities = [f for f in candidates if f["airline"] not in priority_codes]
    # The source query already returns all visible Google cards. This ordering
    # makes the scanner process the requested priority carriers first, so a
    # later CAPTCHA or runtime limit cannot systematically skip them.
    priorities.sort(key=lambda f: (f["price_pln"], f["duration_h"] or 999,
                                   f["stops"] if f["stops"] is not None else 9))
    non_priorities.sort(key=lambda f: (f["price_pln"], f["duration_h"] or 999,
                                       f["stops"] if f["stops"] is not None else 9))
    priority_picks = priorities[:max_options]
    picks = priority_picks + non_priorities[:max(0, max_options - len(priority_picks))]
    return picks
