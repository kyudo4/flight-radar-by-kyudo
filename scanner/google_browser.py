"""Rendered Google Flights fallback used when server HTML no longer has fares.

Google periodically moves flight cards out of the initial ``ds:1`` payload and
loads them with JavaScript.  The normal lightweight collector remains the
first choice; this module starts one reusable headless Chrome instance only
when that response cannot be parsed.
"""

import atexit
import os
import re


class BrowserBlockedError(RuntimeError):
    """The rendered page is a consent/CAPTCHA/block page."""


class BrowserCapacityError(RuntimeError):
    """The bounded browser fallback budget for this run has been used."""


class BrowserParseError(RuntimeError):
    """Chrome loaded the page but no trustworthy flight cards were found."""


_RUNTIME = None
_PLAYWRIGHT = None
_BROWSER = None
_CONTEXT = None
_PAGE = None
_QUERY_COUNT = 0


def _number(value):
    digits = re.sub(r"[^0-9]", "", value or "")
    return int(digits) if digits else None


def parse_card_label(label):
    """Parse Google's accessible flight-card description.

    Accessible labels are intentionally preferred over CSS class names.  The
    labels are the same data exposed to screen readers and have remained much
    more stable than Google's generated class names.
    """
    text = " ".join(str(label or "").split())
    price_match = re.search(
        r"(?:From\s+)?([0-9][0-9 ,.\u00a0]*)\s+(?:Polish zlotys|PLN)\b",
        text,
        re.I,
    )
    duration_match = re.search(
        r"Total duration\s+(?:(\d+)\s*hr)?(?:\s*(\d+)\s*min)?\.",
        text,
        re.I,
    )
    airline_match = re.search(
        r"(?:Nonstop|\d+\s+stops?) flight with (.+?)\.\s+(?:Operated by .+?\.\s+)?Leaves ",
        text,
        re.I,
    )
    departure_match = re.search(
        r"Leaves .+? at (.+?) and arrives at .+? at (.+?)\.\s+Total duration",
        text,
        re.I,
    )
    nonstop = re.search(r"\bNonstop flight\b", text, re.I)
    stops_match = re.search(r"\b(\d+)\s+stops? flight\b", text, re.I)
    if not (price_match and duration_match and airline_match and (nonstop or stops_match)):
        raise BrowserParseError("Niepełna karta dostępności Google Flights")
    hours = int(duration_match.group(1) or 0)
    minutes = int(duration_match.group(2) or 0)
    duration_h = round(hours + minutes / 60, 2)
    price = _number(price_match.group(1))
    if not price or duration_h <= 0:
        raise BrowserParseError("Nieprawidłowa cena lub czas w karcie Google Flights")
    departure = ""
    if departure_match:
        departure = "%s → %s" % (departure_match.group(1), departure_match.group(2))
    return {
        "airline_name": airline_match.group(1).strip(),
        "price_pln": price,
        "duration_h": duration_h,
        "stops": 0 if nonstop else int(stops_match.group(1)),
        "departure": departure,
        "aircraft": "",
        "_label": text,
    }


def _close():
    global _RUNTIME, _PLAYWRIGHT, _BROWSER, _CONTEXT, _PAGE
    for resource in (_PAGE, _CONTEXT, _BROWSER):
        if resource is not None:
            try:
                resource.close()
            except Exception:
                pass
    if _PLAYWRIGHT is not None:
        try:
            _PLAYWRIGHT.stop()
        except Exception:
            pass
    _RUNTIME = _PLAYWRIGHT = _BROWSER = _CONTEXT = _PAGE = None


atexit.register(_close)


def _page():
    global _RUNTIME, _PLAYWRIGHT, _BROWSER, _CONTEXT, _PAGE
    if _PAGE is not None:
        return _PAGE
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise BrowserParseError("Brak modułu Playwright dla awaryjnego odczytu Google") from exc
    try:
        _PLAYWRIGHT = sync_playwright().start()
        try:
            _BROWSER = _PLAYWRIGHT.chromium.launch(
                channel="chrome",
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
        except Exception:
            # Local development may have Playwright Chromium instead of the
            # Chrome channel.  Production runners normally use the first path.
            _BROWSER = _PLAYWRIGHT.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            )
        _CONTEXT = _BROWSER.new_context(
            locale="en-US",
            timezone_id="Europe/Warsaw",
            viewport={"width": 1440, "height": 1100},
        )
        _CONTEXT.add_cookies([{
            "name": "CONSENT", "value": "YES+cb", "domain": ".google.com", "path": "/",
        }])
        _PAGE = _CONTEXT.new_page()
        return _PAGE
    except Exception as exc:
        _close()
        raise BrowserParseError("Nie udało się uruchomić awaryjnego Chrome: %s" % str(exc)[:160]) from exc


def _load_cards(page, url, returning=False):
    """Load cards with one clean retry for Google's occasional empty render."""
    last_error = None
    for attempt in range(2):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=45000)
            heading = "Returning flights" if returning else "Search results"
            try:
                page.get_by_role("heading", name=heading, exact=True).wait_for(timeout=25000)
            except Exception:
                pass
            links = page.get_by_role("link", name=re.compile(r"Select flight", re.I))
            links.first.wait_for(timeout=25000)
            cards = []
            seen = set()
            for index in range(min(links.count(), 120)):
                label = links.nth(index).get_attribute("aria-label") or ""
                if not label or label in seen:
                    continue
                seen.add(label)
                try:
                    cards.append(parse_card_label(label))
                except BrowserParseError:
                    continue
            if cards:
                return cards
            last_error = BrowserParseError(
                "Google zwrócił karty bez pełnej ceny/czasu/przesiadek"
            )
        except BrowserBlockedError:
            raise
        except Exception as exc:
            last_error = exc
            try:
                body = " ".join(page.locator("body").inner_text(timeout=5000).lower().split())
            except Exception:
                body = ""
            if any(marker in body for marker in (
                    "unusual traffic", "not a robot", "captcha", "before you continue")):
                raise BrowserBlockedError("Google pokazał blokadę w awaryjnym Chrome") from exc
        if attempt == 0:
            page.wait_for_timeout(1800)
    direction = "powrotnych" if returning else "wylotowych"
    raise BrowserParseError(
        "Awaryjny Chrome po dwóch próbach nie znalazł kart %s: %s"
        % (direction, str(last_error)[:120])
    ) from last_error


def _outbound_candidates(cards, maximum=3):
    """Pick complementary outbound choices without exploding browser traffic."""
    if not cards:
        return []
    cheapest = min(cards, key=lambda item: (item["price_pln"], item["duration_h"], item["stops"]))
    practical = min(
        cards,
        key=lambda item: (
            item["price_pln"] + max(0, item["duration_h"] - 24) * 350 + item["stops"] * 120,
            item["duration_h"],
            item["price_pln"],
        ),
    )
    shortest = min(cards, key=lambda item: (item["duration_h"], item["price_pln"], item["stops"]))
    selected = []
    for card in (practical, cheapest, shortest):
        if card not in selected:
            selected.append(card)
        if len(selected) >= maximum:
            break
    return selected


def fetch_rendered(url, return_date=None):
    """Return normalized one-way or fully verified round-trip cards."""
    global _QUERY_COUNT
    maximum = max(1, min(200, int(os.environ.get("GOOGLE_BROWSER_QUERY_LIMIT", "80"))))
    if _QUERY_COUNT >= maximum:
        raise BrowserCapacityError(
            "Awaryjny odczyt Chrome osiągnął bezpieczny limit %d zapytań" % maximum
        )
    _QUERY_COUNT += 1
    page = _page()
    outbound_cards = _load_cards(page, url)
    if not return_date:
        for card in outbound_cards:
            card.pop("_label", None)
            card.update({
                "round_trip_verified": True,
                "outbound_duration_h": card["duration_h"],
                "outbound_stops": card["stops"],
                "return_duration_h": None,
                "return_stops": None,
                "return_departure": "",
                "link": url,
            })
        return outbound_cards

    combined = []
    for candidate_index, outbound in enumerate(_outbound_candidates(outbound_cards)):
        # Two complementary outbound choices provide airline variety.  A
        # third is attempted only when both produced no valid return.
        if candidate_index >= 2 and combined:
            break
        # Reload the original list so every candidate is selected from a clean
        # state.  Exact accessible name avoids brittle CSS classes.
        _load_cards(page, url)
        try:
            page.get_by_role("link", name=outbound["_label"], exact=True).click(timeout=15000)
            page.get_by_role("heading", name="Returning flights", exact=True).wait_for(timeout=25000)
            return_url = page.url
            inbound_cards = _load_cards(page, return_url, returning=True)
        except BrowserBlockedError:
            raise
        except Exception:
            continue
        inbound_cards.sort(key=lambda item: (
            item["price_pln"] + max(0, item["duration_h"] - 24) * 350 + item["stops"] * 120,
            item["price_pln"], item["duration_h"],
        ))
        for inbound in inbound_cards[:4]:
            airlines = outbound["airline_name"]
            if inbound["airline_name"].lower() not in airlines.lower():
                airlines += " / " + inbound["airline_name"]
            combined.append({
                "airline_name": airlines,
                "price_pln": inbound["price_pln"],
                "duration_h": max(outbound["duration_h"], inbound["duration_h"]),
                "stops": max(outbound["stops"], inbound["stops"]),
                "departure": outbound["departure"],
                "aircraft": "",
                "round_trip_verified": True,
                "outbound_duration_h": outbound["duration_h"],
                "outbound_stops": outbound["stops"],
                "return_duration_h": inbound["duration_h"],
                "return_stops": inbound["stops"],
                "return_departure": inbound["departure"],
                "link": return_url,
            })
    if not combined:
        raise BrowserParseError("Nie udało się potwierdzić odcinka powrotnego w Google")
    return combined
