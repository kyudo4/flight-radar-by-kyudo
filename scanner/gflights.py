# -*- coding: utf-8 -*-
"""
Źródło Google Flights — nieoficjalne, bez przeglądarki.

Buduje parametr tfs (protobuf) przez fast-flights, pobiera stronę z cookie
CONSENT=YES+cb (omija unijną ścianę zgody — Google renderuje wtedy wyniki
po stronie serwera) i parsuje HTML parserem fast-flights.
"""

import re
import urllib.error
import urllib.parse
import urllib.request

from fast_flights import FlightData, Passengers
from fast_flights.filter import create_filter
from fast_flights.core import parse_response

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


class _FakeResponse(object):
    def __init__(self, status_code, text):
        self.status_code = status_code
        self.text = text
        self.text_markdown = text[:2000]


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


def _parse_duration_h(s):
    h = re.search(r"(\d+)\s*hr", s or "")
    m = re.search(r"(\d+)\s*min", s or "")
    if not h and not m:
        return None
    return round(int(h.group(1) if h else 0) + int(m.group(1) if m else 0) / 60, 1)


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


def fetch_gf(origin, dest, date, seat="business", return_date=None, timeout=35):
    """Zwraca (price_level, [dict na lot]). Rzuca BlockedError przy blokadzie."""
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
        # 403/429/503 są traktowane jako blokada lub throttling Google.
        # Nie ponawiamy ich w tym samym przebiegu, żeby nie pogłębiać blokady.
        if exc.code in {403, 429, 503}:
            raise BlockedError("Google HTTP %s" % exc.code) from exc
        raise
    if status != 200 or "Before you continue" in body[:200000]:
        raise BlockedError("consent wall / status %s" % status)
    try:
        result = parse_response(_FakeResponse(status, body))
    except RuntimeError:
        return None, []  # brak lotów na tej trasie/dacie — to nie błąd
    flights = []
    for fl in result.flights:
        price = _parse_price_pln(fl.price)
        if not price:
            continue
        stops = fl.stops if isinstance(fl.stops, int) else None
        if stops is None:
            stop_match = re.search(r"(\d+)\s*stop", str(fl.stops or ""), re.I)
            stops = int(stop_match.group(1)) if stop_match else (0 if re.search(r"non[- ]?stop|direct", str(fl.stops or ""), re.I) else None)
        duration_h = _parse_duration_h(fl.duration)
        if duration_h is None or stops is None:
            continue
        flights.append({
            "airline_name": fl.name,
            "airline": airline_code(fl.name),
            "price_pln": price,
            "duration_h": duration_h,
            "stops": stops,
            "departure": fl.departure,
            "aircraft": getattr(fl, "aircraft", "") or getattr(fl, "aircraft_type", ""),
            "link": url,
        })
    return result.current_price, flights


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
    picks = candidates[:max_options]
    priorities = [f for f in candidates if f["airline"] in priority_codes]
    if priorities:
        best_priority = min(priorities, key=lambda f: (f["price_pln"],
                                                       f["duration_h"] or 999))
        if best_priority not in picks:
            picks.append(best_priority)
    return picks
