"""Lekki kolektor RSS dla świeżych promocji i error fares."""
import html
import json
import re
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from email.utils import parsedate_to_datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent
FEEDS = json.loads((ROOT / "feeds.json").read_text())
ORIGINS = {
    "GDN": ["gdańsk", "gdansk"], "WAW": ["warsaw", "warszawa"], "POZ": ["poznań", "poznan"],
    "OSL": ["oslo"], "ARN": ["stockholm", "sztokholm"], "CPH": ["copenhagen", "kopenhaga"],
    "VIE": ["vienna", "wien"], "BUD": ["budapest"], "MXP": ["milan", "milano"], "IST": ["istanbul", "stambuł", "stambul"]}
DESTS = {
    "BKK": ["bangkok", "bkk"], "SIN": ["singapore", "singapur", "sin"], "KUL": ["kuala lumpur", "kul"],
    "HKG": ["hong kong", "hkg"], "HAN": ["hanoi", "han"], "SGN": ["ho chi minh", "saigon", "sgn"],
    "HND": ["tokyo", "haneda", "hnd"], "NRT": ["narita", "nrt"], "ICN": ["seoul", "incheon", "icn"]}
AIRLINES = {"qatar": "QR", "etihad": "EY", "emirates": "EK", "oman air": "WY", "turkish": "TK", "eva air": "BR", "singapore airlines": "SQ", "cathay": "CX", "ana": "NH", "japan airlines": "JL", "air china": "CA"}
MONTHS = {"january":1,"february":2,"march":3,"april":4,"may":5,"june":6,"july":7,"august":8,"september":9,"october":10,"november":11,"december":12,"września":9,"wrzesień":9}


def clean(value):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", value or ""))).strip()


def fresh(value):
    if not value:
        return True
    try:
        parsed = parsedate_to_datetime(value)
        return datetime.now(parsed.tzinfo) - parsed <= timedelta(days=21)
    except (TypeError, ValueError):
        return True


def items(feed):
    try:
        req = urllib.request.Request(feed["url"], headers={"User-Agent": "FlightRadarByKyudo/2.0"})
        with urllib.request.urlopen(req, timeout=20) as response:
            root = ET.fromstring(response.read())
        result = []
        for node in root.iter("item"):
            if fresh(node.findtext("pubDate")):
                result.append({"title": clean(node.findtext("title")), "description": clean(node.findtext("description")), "link": (node.findtext("link") or "").strip(), "source": feed["name"]})
        atom = "{http://www.w3.org/2005/Atom}"
        for node in root.iter(atom + "entry"):
            if not fresh(node.findtext(atom + "updated") or node.findtext(atom + "published")):
                continue
            link = node.find(atom + "link")
            result.append({"title": clean(node.findtext(atom + "title")), "description": clean(node.findtext(atom + "summary") or node.findtext(atom + "content")), "link": link.get("href", "") if link is not None else "", "source": feed["name"]})
        return result[:60]
    except Exception:
        return []


def dates_in_range(text, filters):
    """Zwraca tylko konkretne dni. Sam napis 'we wrześniu' nie przechodzi."""
    text = (text or "").lower()
    start = filters.get("from", "")
    end = filters.get("to", start)
    if not start: return []
    start_date = datetime.strptime(start, "%Y-%m-%d").date(); end_date = datetime.strptime(end, "%Y-%m-%d").date()
    year = start_date.year
    years = {int(x) for x in re.findall(r"\b(20\d{2})\b", text)}
    if years and year not in years: return []
    found = set()
    for y, m, d in re.findall(r"\b(20\d{2})[-./](\d{1,2})[-./](\d{1,2})\b", text):
        found.add((int(y), int(m), int(d)))
    for d, m, y in re.findall(r"\b(\d{1,2})[./](\d{1,2})(?:[./](20\d{2}))?\b", text):
        found.add((int(y or year), int(m), int(d)))
    for name, month in MONTHS.items():
        for d in re.findall(r"\b(\d{1,2})\s+" + name + r"\b|\b" + name + r"\s+(\d{1,2})\b", text):
            day = int(d[0] or d[1]); found.add((year, month, day))
    result = []
    for y, m, d in found:
        try:
            candidate = datetime(y, m, d).date()
            if start_date <= candidate <= end_date: result.append(candidate.isoformat())
        except ValueError: pass
    return sorted(set(result))


RATES_TO_PLN = {"PLN": 1.0, "ZŁ": 1.0, "EUR": 4.35, "€": 4.35, "USD": 3.80, "$": 3.80, "GBP": 5.10, "£": 5.10}


def _number(raw):
    raw = re.sub(r"\s+", "", raw)
    if "," in raw and "." in raw:
        # Wpisy typu 1,299.00 albo 1.299,00.
        decimal = "." if raw.rfind(".") > raw.rfind(",") else ","
        thousands = "," if decimal == "." else "."
        raw = raw.replace(thousands, "").replace(decimal, ".")
    elif "," in raw:
        parts = raw.split(",")
        raw = "".join(parts) if len(parts[-1]) == 3 else ".".join(parts)
    elif raw.count(".") > 1:
        raw = raw.replace(".", "")
    return float(raw)


def price(text):
    currency = r"PLN|zł|EUR|€|USD|\$|GBP|£"
    pattern = rf"(?:(?P<before>{currency})\s*(?P<left>\d[\d\s.,]*)|(?P<right>\d[\d\s.,]*)\s*(?P<after>{currency}))"
    values = []
    for match in re.finditer(pattern, text, re.I):
        unit = (match.group("before") or match.group("after")).upper()
        raw = match.group("left") or match.group("right")
        try:
            amount = _number(raw)
            value = amount * RATES_TO_PLN.get(unit, 1.0)
            if 300 <= value <= 40000:
                values.append(round(value))
        except ValueError:
            pass
    return min(values) if values else None


def candidates(monitors):
    out = []
    for feed in FEEDS:
        for item in items(feed):
            text = (item["title"] + " " + item["description"]).lower()
            if not re.search(r"business|first|premium fare|lie[- ]flat", text):
                continue
            if re.search(r"miles|points|award ticket|loyalty points|mile redemption", text):
                continue
            destinations = [code for code, words in DESTS.items() if any(w in text for w in words)]
            origins = [code for code, words in ORIGINS.items() if any(w in text for w in words)]
            if not destinations or not origins:
                continue
            cabin = "FIRST" if re.search(r"first class|first fare", text) else "BUSINESS"
            carrier = next((code for name, code in AIRLINES.items() if name in text), "")
            amount = price(text)
            if not amount:
                continue
            tags = []
            if re.search(r"error fare|mistake fare|error price", text): tags.append("Error Fare")
            if re.search(r"flash sale|limited time|ends soon", text): tags.append("Flash Sale")
            if re.search(r"promo code|coupon|discount code", text): tags.append("Promo Code")
            if re.search(r"companion", text): tags.append("Companion Fare")
            for monitor in monitors:
                filters = monitor.get("filters") or {}
                if cabin != filters.get("cabin", cabin):
                    continue
                if not set(origins) & set(filters.get("origins", [])) or not set(destinations) & set(filters.get("destinations", [])):
                    continue
                dates = dates_in_range(text, filters)
                if not dates:
                    continue
                for origin in sorted(set(origins) & set(filters.get("origins", []))):
                    for dest in sorted(set(destinations) & set(filters.get("destinations", []))):
                        out.append((monitor, {"airline": carrier, "airline_name": next((name.title() for name, code in AIRLINES.items() if code == carrier), ""), "price_pln": amount, "duration_h": None, "stops": None, "departure": "", "link": item["link"], "source": item["source"], "tags": tags, "title": item["title"]}, origin, dest, dates[0]))
    return out
