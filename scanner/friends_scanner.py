#!/usr/bin/env python3
"""Wspólny skaner Flight Radar by Kyudo: stan i dane są wyłącznie w Supabase."""
import hashlib
import html
import json
import math
import os
import statistics
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

import gflights
import rss
import telegram_io

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
INITIAL_STANDARD = max(1, min(1000, int(os.environ.get("MAX_STANDARD_QUERIES", "240"))))
INITIAL_FIRST = max(1, min(100, int(os.environ.get("MAX_FIRST_QUERIES", "12"))))
MAX_STANDARD = INITIAL_STANDARD
MAX_FIRST = INITIAL_FIRST
STANDARD_CEILING = max(INITIAL_STANDARD, min(1000, int(os.environ.get("MAX_STANDARD_CEILING", "400"))))
FIRST_CEILING = max(INITIAL_FIRST, min(100, int(os.environ.get("MAX_FIRST_CEILING", "24"))))
STANDARD_STEP = max(1, min(200, int(os.environ.get("QUERY_RAMP_STANDARD_STEP", "40"))))
FIRST_STEP = max(1, min(50, int(os.environ.get("QUERY_RAMP_FIRST_STEP", "2"))))
STANDARD_FLOOR = max(1, min(INITIAL_STANDARD, int(os.environ.get("QUERY_BLOCK_FLOOR_STANDARD", "60"))))
FIRST_FLOOR = max(1, min(INITIAL_FIRST, int(os.environ.get("QUERY_BLOCK_FLOOR_FIRST", "4"))))
MAX_AIRPORTS_PER_SIDE = 5
MAX_MONITOR_COMBINATIONS = 5000
SCAN_INTERVAL_HOURS = 6
FORCE_SCAN = os.environ.get("FORCE_SCAN", "false").lower() == "true"
FULL_QUEUE_SCAN = os.environ.get("FULL_QUEUE_SCAN", "false").lower() == "true"
FULL_QUEUE_STANDARD_LIMIT = max(1, min(1000, int(os.environ.get("FULL_QUEUE_STANDARD_LIMIT", "400"))))
FULL_QUEUE_FIRST_LIMIT = max(1, min(100, int(os.environ.get("FULL_QUEUE_FIRST_LIMIT", "40"))))
PROCESS_TELEGRAM_ONLY = os.environ.get("PROCESS_TELEGRAM_ONLY", "false").lower() == "true"
RESERVED_RUN_ID = os.environ.get("RESERVED_RUN_ID", "").strip()
REQUEST_DELAY_SECONDS = max(0.5, min(5.0, float(os.environ.get("GOOGLE_REQUEST_DELAY_SECONDS", "1.5"))))
MAX_SCAN_RUNTIME_SECONDS = max(60, min(21600, int(os.environ.get("MAX_SCAN_RUNTIME_SECONDS", "3000"))))
PROGRESS_UPDATE_EVERY = max(1, min(50, int(os.environ.get("SCAN_PROGRESS_EVERY", "5"))))
FETCH_RETRIES = 2
PREFERENCE_FETCH_RETRIES = 3
STALE_AFTER_HOURS = 24
PRICE_HISTORY_LAST = {}
STALE_SCHEMA_SUPPORTED = None
PRIORITY = {"QR", "EY", "EK", "WY", "TK", "BR", "SQ", "CX", "NH", "JL"}
AIRPORTS_FILE = Path(__file__).resolve().parents[1] / "site" / "airports.json"


def load_airport_codes():
    try:
        return set(json.loads(AIRPORTS_FILE.read_text(encoding="utf-8")))
    except (OSError, ValueError, TypeError):
        return set()


VALID_AIRPORT_CODES = load_airport_codes()


def log(text):
    print("[%s] %s" % (datetime.utcnow().isoformat(timespec="seconds"), text))


def update_scan_progress(run_id, query_count, offer_count):
    """Expose bounded progress without making a failed status write fatal."""
    try:
        api("PATCH", "scan_runs", body={
            "query_count": query_count,
            "offer_count": offer_count,
            "status": "running",
        }, params={"id": "eq." + run_id})
    except Exception as exc:
        log("Nie udało się zapisać postępu skanu: %s" % str(exc)[:120])


def api(method, path, body=None, params=None):
    if not SUPABASE_URL or not SERVICE_KEY:
        raise RuntimeError("Brak SUPABASE_URL albo SUPABASE_SERVICE_ROLE_KEY")
    url = SUPABASE_URL + "/rest/v1/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("apikey", SERVICE_KEY)
    req.add_header("Authorization", "Bearer " + SERVICE_KEY)
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json")
    if body is not None:
        preference = "return=representation"
        if params and "on_conflict" in params:
            preference = "resolution=merge-duplicates,return=representation"
        req.add_header("Prefer", preference)
    with urllib.request.urlopen(req, timeout=30) as resp:
        raw = resp.read()
        return json.loads(raw) if raw else []


def telegram(method, payload):
    if not TG_TOKEN:
        return None
    req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (TG_TOKEN, method), data=json.dumps(payload).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=25) as resp:
        return json.loads(resp.read())


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def date_range(filters):
    start = parse_date(filters.get("from"))
    end = parse_date(filters.get("to") or filters.get("from"))
    if end < start:
        return []
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def trip_type(filters):
    return "round_trip" if str((filters or {}).get("trip_type") or "one_way").lower() == "round_trip" else "one_way"


def valid_return_dates(filters, departure):
    """Zwraca tylko powroty zgodne z zakresem i długością pobytu."""
    if trip_type(filters) != "round_trip":
        return [None]
    return_from = parse_date(filters.get("return_from"))
    return_to = parse_date(filters.get("return_to") or filters.get("return_from"))
    return [candidate for candidate in (return_from + timedelta(days=i) for i in range((return_to - return_from).days + 1))
            if (candidate - departure).days >= 1]


def monitor_combination_count(filters):
    """Zwraca bezpieczny górny limit kombinacji przed materializacją kolejki."""
    origins = {str(x).upper() for x in (filters or {}).get("origins", []) if x}
    destinations = {str(x).upper() for x in (filters or {}).get("destinations", []) if x}
    try:
        departure_days = (parse_date(filters["to"]) - parse_date(filters["from"])).days + 1
        cabins = (filters.get("cabins") if isinstance(filters.get("cabins"), list)
                  else [filters.get("cabin") or "BUSINESS"])
        cabin_count = len({str(value).upper() for value in cabins if value})
        if trip_type(filters) == "round_trip":
            return (len(origins) * len(destinations) * departure_days *
                    ((parse_date(filters["return_to"]) - parse_date(filters["return_from"])).days + 1) *
                    max(1, cabin_count))
        return len(origins) * len(destinations) * departure_days * max(1, cabin_count)
    except (KeyError, TypeError, ValueError):
        return 0


def monitor_combinations(monitor):
    f = monitor.get("filters") or {}
    origins = sorted({x.upper() for x in f.get("origins", []) if x})
    destinations = sorted({x.upper() for x in f.get("destinations", []) if x})
    dates = [day for day in date_range(f) if day >= date.today()]
    raw_cabins = f.get("cabins") if isinstance(f.get("cabins"), list) else [f.get("cabin") or "BUSINESS"]
    cabins = sorted({str(value).lower().replace("_", "-") for value in raw_cabins if str(value).upper() in {"BUSINESS", "FIRST", "PREMIUM_ECONOMY", "ECONOMY"}})
    if (not origins or not destinations or not dates
            or len(origins) > MAX_AIRPORTS_PER_SIDE
            or len(destinations) > MAX_AIRPORTS_PER_SIDE
            or monitor_combination_count(f) > MAX_MONITOR_COMBINATIONS
            or (VALID_AIRPORT_CODES and any(code not in VALID_AIRPORT_CODES for code in origins + destinations))):
        return []
    return [{"monitor_id": monitor["id"], "origin": origin, "destination": destination,
             "travel_date": day.isoformat(),
             "return_date": return_date.isoformat() if return_date else None,
             "trip_type": trip_type(f), "cabin": cabin}
            for origin in origins for destination in destinations for day in dates
            for return_date in valid_return_dates(f, day) for cabin in cabins]


def task_from_item(item):
    return {"origin": item["origin"], "dest": item["destination"], "date": item["travel_date"],
            "return_date": item.get("return_date"), "trip_type": item.get("trip_type") or ("round_trip" if item.get("return_date") else "one_way"),
            "cabin": item["cabin"], "item_ids": [item["id"]], "monitor_ids": [item["monitor_id"]],
            "user_ids": [item["user_id"]]}


def sync_monitor_scan_items(monitor):
    """Materializuje każdą kombinację monitora jako niezależną pozycję kolejki."""
    desired = monitor_combinations(monitor)
    try:
        # Database-side reconciliation is atomic and idempotent.  It prevents
        # a concurrent monitor update from producing a false HTTP 409.
        api("POST", "rpc/sync_monitor_scan_items", body={
            "p_monitor_id": monitor["id"],
            "p_items": desired,
        })
        return
    except urllib.error.HTTPError as exc:
        # Additive deployment: keep the old path working until migration 008
        # reaches production.  Other HTTP errors must remain visible.
        if exc.code not in {400, 404}:
            raise
    existing = fetch_all_rows("monitor_scan_items", {
        "monitor_id": "eq." + monitor["id"],
        "select": "id,origin,destination,travel_date,return_date,trip_type,cabin",
        "order": "created_at.asc,id.asc",
    })
    desired_keys = {(x["origin"], x["destination"], x["travel_date"], x.get("return_date"), x["trip_type"], x["cabin"]) for x in desired}
    stale = [x["id"] for x in existing if (x["origin"], x["destination"], x["travel_date"], x.get("return_date"), x.get("trip_type") or ("round_trip" if x.get("return_date") else "one_way"), x["cabin"]) not in desired_keys]
    for offset in range(0, len(stale), 500):
        ids = ",".join(stale[offset:offset + 500])
        if ids:
            api("DELETE", "monitor_scan_items", params={"id": "in.(%s)" % ids})
    existing_keys = {(x["origin"], x["destination"], x["travel_date"], x.get("return_date"), x.get("trip_type") or ("round_trip" if x.get("return_date") else "one_way"), x["cabin"]) for x in existing}
    missing = [x for x in desired if (x["origin"], x["destination"], x["travel_date"], x.get("return_date"), x["trip_type"], x["cabin"]) not in existing_keys]
    for offset in range(0, len(missing), 500):
        batch = missing[offset:offset + 500]
        if not batch:
            continue
        # Missing rows were resolved against the complete key above. The
        # one-way and round-trip partial indexes intentionally have different
        # conflict targets, so a plain insert is safer than pretending there
        # is one universal PostgREST on_conflict target.
        try:
            api("POST", "monitor_scan_items", body=batch)
        except urllib.error.HTTPError as exc:
            if exc.code != 409:
                raise
            # Rare race with an edit trigger: retry individually and accept a
            # 409 only after the exact queue row is confirmed to exist.
            for item in batch:
                try:
                    api("POST", "monitor_scan_items", body=item)
                except urllib.error.HTTPError as item_exc:
                    if item_exc.code != 409 or not _scan_item_exists(item):
                        raise


def _scan_item_exists(item):
    params = {
        "monitor_id": "eq." + item["monitor_id"],
        "origin": "eq." + item["origin"],
        "destination": "eq." + item["destination"],
        "travel_date": "eq." + item["travel_date"],
        "trip_type": "eq." + item["trip_type"],
        "cabin": "eq." + item["cabin"],
        "return_date": ("eq." + item["return_date"]) if item.get("return_date") else "is.null",
        "select": "id",
        "limit": "1",
    }
    return bool(api("GET", "monitor_scan_items", params=params))


class ScanBlockedRun(RuntimeError):
    """Służy do oznaczenia blokady Google jako failed w GitHub Actions."""


class ScanSourceRun(RuntimeError):
    """Oznacza zmianę/awarię formatu źródła, a nie blokadę ruchu."""


def force_due_scan_items(monitors, now):
    """Ręczny workflow omija oczekiwanie na kolejny zaplanowany cykl."""
    monitor_ids = [monitor["id"] for monitor in monitors if monitor.get("id")]
    if not monitor_ids:
        return
    stamp = now.isoformat() + "Z"
    ids = ",".join(monitor_ids)
    api("PATCH", "monitor_scan_items", body={"next_scan_at": stamp},
        params={"monitor_id": "in.(%s)" % ids})
    api("PATCH", "monitors", body={"next_scan_at": stamp},
        params={"id": "in.(%s)" % ids})


def fetch_due_scan_items(active_ids, now):
    if not active_ids:
        return []
    base_params = {
        "monitor_id": "in.(%s)" % ",".join(active_ids),
        "or": "(next_scan_at.is.null,next_scan_at.lte.%s)" % (now.isoformat() + "Z"),
        "select": "*",
        "order": "next_scan_at.asc,created_at.asc,id.asc",
    }
    # Pobieramy kolejkę stronami, żeby duża liczba monitorów nie obcinała
    # późniejszych użytkowników na stałym limicie jednego zapytania REST.
    # Supabase/PostgREST commonly caps a single response at 1000 rows even if
    # a larger limit is requested. Using that size guarantees the next page is
    # fetched instead of mistaking the server cap for the end of the queue.
    return fetch_all_rows("monitor_scan_items", base_params, page_size=1000)


def fetch_all_rows(path, params, page_size=1000):
    """Read every PostgREST page so history-dependent rules stay durable."""
    result = []
    offset = 0
    while True:
        page = api("GET", path, params={**params, "limit": str(page_size), "offset": str(offset)})
        result.extend(page)
        if len(page) < page_size:
            return result
        offset += page_size


def adaptive_query_limits():
    """Dobiera limit na ten przebieg i reaguje na opór Google.

    Zdrowe przebiegi zwiększają limit małymi krokami. Pierwszy 409/403/429/503
    albo consent/CAPTCHA zapisuje przebieg jako zablokowany, a następny start
    schodzi co najmniej do bezpiecznego poziomu. Dzięki temu limit nie jest
    bezmyślnie podbijany po blokadzie i nie ma ponawiania zablokowanego żądania.
    """
    if FULL_QUEUE_SCAN:
        # Jednorazowe przejście administracyjne nie korzysta z rampowania ani
        # z poprzedniego limitu po blokadzie. Nadal obowiązuje bezpieczne
        # zatrzymanie po CAPTCHA/błędzie struktury w pętli pobierania.
        return {"standard": FULL_QUEUE_STANDARD_LIMIT, "first": FULL_QUEUE_FIRST_LIMIT}
    limits = {"standard": INITIAL_STANDARD, "first": INITIAL_FIRST}
    try:
        recent = api("GET", "scan_runs", params={
            "select": "status,blocked,standard_limit,first_limit,started_at",
            "status": "not.in.(queued,running)",
            "finished_at": "not.is.null",
            "order": "started_at.desc", "limit": "6",
        })
    except Exception as exc:
        log("Nie udało się odczytać historii rampowania; używam limitu startowego: %s" % str(exc)[:120])
        return {"standard": STANDARD_FLOOR, "first": FIRST_FLOOR}
    if not recent:
        return limits
    latest = recent[0]
    limits["standard"] = max(STANDARD_FLOOR, min(STANDARD_CEILING, int(latest.get("standard_limit") or INITIAL_STANDARD)))
    limits["first"] = max(FIRST_FLOOR, min(FIRST_CEILING, int(latest.get("first_limit") or INITIAL_FIRST)))
    if latest.get("blocked") or latest.get("status") == "blocked":
        return {"standard": max(STANDARD_FLOOR, limits["standard"] // 2),
                "first": max(FIRST_FLOOR, limits["first"] // 2)}
    healthy = [row for row in recent[:3] if row.get("status") == "ok" and not row.get("blocked")]
    if len(healthy) == 3:
        limits["standard"] = min(STANDARD_CEILING, limits["standard"] + STANDARD_STEP)
        limits["first"] = min(FIRST_CEILING, limits["first"] + FIRST_STEP)
    return limits


def mark_stale_offers():
    """Oznacza ceny, których Google nie potwierdził przez dobę jako nieaktualne."""
    global STALE_SCHEMA_SUPPORTED
    if STALE_SCHEMA_SUPPORTED is False:
        return
    cutoff = (datetime.utcnow() - timedelta(hours=STALE_AFTER_HOURS)).isoformat() + "Z"
    try:
        # Older Supabase projects may not have applied the quality migration
        # yet. Detect that once and keep scanning with the legacy offer shape
        # instead of emitting the same HTTP 400 on every run.
        api("GET", "flight_offers", params={
            "select": "id,verification_status",
            "limit": "1",
        })
        STALE_SCHEMA_SUPPORTED = True
        api("PATCH", "flight_offers", body={"verification_status": "stale", "verification_note": "Brak potwierdzenia ceny od ponad 24 godzin"},
            params={"last_seen_at": "lt." + cutoff, "verification_status": "neq.stale"})
    except urllib.error.HTTPError as exc:
        if exc.code == 400:
            STALE_SCHEMA_SUPPORTED = False
            log("Pomijam oznaczanie starych ofert: baza nie ma jeszcze migracji jakości ofert")
            return
        raise
    except Exception as exc:
        # Czyszczenie pomocnicze nie może zatrzymać skanowania ofert.
        log("Nie udało się oznaczyć starych ofert: %s" % str(exc)[:120])


def fetch_task(task):
    """Ponawia tylko błędy sieciowe; blokadę Google zgłasza od razu."""
    last_error = None
    for attempt in range(FETCH_RETRIES):
        try:
            return gflights.fetch_gf(task["origin"], task["dest"], task["date"], seat=task["cabin"], return_date=task.get("return_date"))
        except gflights.BlockedError:
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last_error = exc
            if attempt + 1 < FETCH_RETRIES:
                time.sleep(2 ** attempt)
    raise last_error


def _select_fair_bucket(items, max_count, allowed_cabins):
    """Wybiera unikalne zapytania rotacyjnie po klasie i monitorze.

    Rotacja po klasie jest dynamiczna: gdy w kolejce występują Economy i
    Business, obie dostają kolejno miejsce, ale gdy istnieje tylko jedna klasa,
    wykorzystuje cały limit. Powiązane monitory dla identycznego zapytania są
    zwracane razem, więc deduplikacja nie odbiera wyniku żadnemu użytkownikowi.
    """
    if max_count <= 0:
        return []
    by_cabin_monitor = defaultdict(lambda: defaultdict(list))
    by_key = defaultdict(list)
    for item in items:
        cabin = str(item.get("cabin") or "").lower().replace("_", "-")
        if cabin not in allowed_cabins:
            continue
        key = "%s|%s|%s|%s|%s" % (item["origin"], item["destination"], item["travel_date"], item.get("return_date") or "", cabin)
        by_cabin_monitor[cabin][item["monitor_id"]].append((key, item))
        by_key[key].append(item)

    cabin_priority = {"business": 0, "economy": 1, "premium-economy": 2, "first": 3}
    cabins = sorted(by_cabin_monitor, key=lambda cabin: (cabin_priority.get(cabin, 99), cabin))
    monitor_ids = {cabin: sorted(by_cabin_monitor[cabin]) for cabin in cabins}
    monitor_cursor = {cabin: 0 for cabin in cabins}
    positions = {cabin: {monitor_id: 0 for monitor_id in monitor_ids[cabin]} for cabin in cabins}
    selected_keys = []
    selected_key_set = set()

    while len(selected_keys) < max_count and cabins:
        progress = False
        exhausted = []
        for cabin in cabins:
            if len(selected_keys) >= max_count:
                break
            ids = monitor_ids[cabin]
            picked = False
            for _ in range(len(ids)):
                monitor_id = ids[monitor_cursor[cabin]]
                monitor_cursor[cabin] = (monitor_cursor[cabin] + 1) % len(ids)
                entries = by_cabin_monitor[cabin][monitor_id]
                while positions[cabin][monitor_id] < len(entries):
                    key, item = entries[positions[cabin][monitor_id]]
                    positions[cabin][monitor_id] += 1
                    if key in selected_key_set:
                        continue
                    selected_key_set.add(key)
                    selected_keys.append(key)
                    progress = picked = True
                    break
                if picked or len(selected_keys) >= max_count:
                    break
            if not picked and all(positions[cabin][monitor_id] >= len(by_cabin_monitor[cabin][monitor_id]) for monitor_id in ids):
                exhausted.append(cabin)
        cabins = [cabin for cabin in cabins if cabin not in exhausted]
        if not progress:
            break

    return [item for key in selected_keys for item in by_key[key]]


def select_scan_items(due_items, max_standard=MAX_STANDARD, max_first=MAX_FIRST):
    """Wybiera zadania fair-use po monitorze i klasie, bez głodzenia Economy."""
    standard = _select_fair_bucket(due_items, max_standard, {"business", "economy", "premium-economy"})
    first = _select_fair_bucket(due_items, max_first, {"first"})
    return standard + first


def task_key(task):
    return "%s|%s|%s|%s|%s" % (task["origin"], task["dest"], task["date"], task.get("return_date") or "", task["cabin"])


def offer_fingerprint(task, flight):
    raw = "%s|%s|%s|%s|%s|%s|%s|%s|%s" % (task["origin"], task["dest"], task["date"], task.get("return_date") or "", task.get("trip_type") or ("round_trip" if task.get("return_date") else "one_way"), task["cabin"], flight.get("airline", ""), flight.get("departure", ""), flight.get("duration_h", ""))
    return hashlib.sha1(raw.encode()).hexdigest()


def quality(flight, filters):
    trip = trip_type(filters)
    if trip == "round_trip" and not flight.get("round_trip_verified", False):
        # A round-trip result without a confirmed inbound leg cannot be
        # proven to satisfy the same time/stop rules in both directions.
        return False
    raw_max_duration = filters.get("max_duration_h")
    if raw_max_duration in (None, ""):
        max_duration = None
    else:
        try:
            max_duration = float(raw_max_duration)
        except (TypeError, ValueError):
            return False
    duration_values = [flight.get("outbound_duration_h") or flight.get("duration_h")]
    if trip == "round_trip":
        duration_values.append(flight.get("return_duration_h"))
    if max_duration is not None and any(value is None or float(value) > max_duration for value in duration_values):
        return False
    max_stops = filters.get("max_stops")
    stops_values = [flight.get("outbound_stops") if flight.get("outbound_stops") is not None else flight.get("stops")]
    if trip == "round_trip":
        stops_values.append(flight.get("return_stops"))
    if max_stops is not None and any(value is None or int(value) > int(max_stops) for value in stops_values):
        return False
    if filters.get("direct_only") and any(value != 0 for value in stops_values):
        return False
    excluded = [str(x).lower() for x in filters.get("excluded_airlines", [])]
    return not any(x in (flight.get("airline_name") or "").lower() for x in excluded)


def budget_ok(flight, filters):
    """Budżet monitora jest zawsze twardym limitem ceny."""
    price = flight.get("price_pln")
    if price is None:
        return False
    budget = float(filters.get("budget_pln") or 999999)
    return float(price) <= budget


def preference_adjustment(flight, filters, preferences=None, route="", destination="", cabin=""):
    """Translate durable cross-monitor preference signals into a star adjustment."""
    if preferences is None:
        return 0
    airline = airline_identity(flight)
    durations = [flight.get("duration_h"), flight.get("outbound_duration_h"), flight.get("return_duration_h")]
    known_durations = [float(value) for value in durations if value is not None and float(value) > 0]
    duration_bucket = int(math.ceil(max(known_durations) / 2) * 2) if known_durations else None
    budget = float(filters.get("budget_pln") or 0)
    price = float(flight.get("price_pln") or 0)
    price_bucket = max(10, min(200, int(math.ceil((price / budget) * 10) * 10))) if budget > 0 and price > 0 else None
    total = 0
    for signal in preferences:
        signal_cabin = str(signal.get("cabin") or "*").upper().replace("-", "_")
        if signal_cabin not in {"*", str(cabin or "").upper().replace("-", "_")}:
            continue
        dimension = str(signal.get("dimension") or "")
        value = str(signal.get("value") or "")
        signal_score = int(signal.get("score") or 0)
        if dimension == "airline" and value == airline:
            total += signal_score
        elif dimension == "route" and value == route:
            total += signal_score
        elif dimension == "destination" and value.upper() == str(destination or "").upper():
            total += signal_score
        elif dimension == "duration" and duration_bucket is not None:
            try:
                learned_bucket = int(value)
            except ValueError:
                continue
            if (signal_score < 0 and duration_bucket >= learned_bucket) or (signal_score > 0 and abs(duration_bucket - learned_bucket) <= 2):
                total += signal_score
        elif dimension == "price" and price_bucket is not None:
            try:
                learned_bucket = int(value)
            except ValueError:
                continue
            if (signal_score < 0 and price_bucket >= learned_bucket) or (signal_score > 0 and price_bucket <= learned_bucket):
                total += signal_score
    if total >= 6:
        return 2
    if total >= 2:
        return 1
    if total <= -6:
        return -2
    if total <= -2:
        return -1
    return 0


def market_price_reference(market_prices):
    """Return a route/cabin benchmark only when enough live observations exist.

    One isolated fare is not a market benchmark. Three or more observations
    from the same Google query or the same route history are the minimum
    needed to avoid turning a sparse result into an overconfident five-star
    rating.
    """
    values = []
    for value in market_prices or []:
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            values.append(number)
    return statistics.median(values) if len(values) >= 3 else None


def market_price_stars(price, market_prices):
    reference = market_price_reference(market_prices)
    if not reference or price <= 0:
        return 0
    ratio = float(price) / reference
    if ratio <= 0.65:
        return 5
    if ratio <= 0.82:
        return 4
    if ratio <= 1.00:
        return 3
    if ratio <= 1.15:
        return 2
    return 1


def score(flight, filters, feedback=None, preferences=None, route="", destination="", cabin="", market_prices=None):
    price = flight.get("price_pln") or 999999
    budget = int(filters.get("budget_pln") or 999999)
    stars = 5 if price <= budget * .55 else 4 if price <= budget * .75 else 3 if price <= budget else 2 if price <= budget * 1.15 else 1
    # A priority carrier is only a small preference bonus, not proof that the
    # fare is good. The route/cabin market comparison can independently
    # upgrade a genuinely cheap fare operated by a non-priority airline.
    if flight.get("airline") in PRIORITY:
        stars = min(5, stars + 1)
    stars = max(stars, market_price_stars(price, market_prices))
    preferred = {str(value).strip().upper() for value in filters.get("preferred_airlines", []) if str(value).strip()}
    airline_code = str(flight.get("airline") or "").strip().upper()
    airline_name = str(flight.get("airline_name") or "").strip().upper()
    if preferred and any(value == airline_code or value in airline_name for value in preferred):
        stars = min(5, stars + 1)
    if flight.get("duration_h") and flight["duration_h"] > 20:
        stars = max(1, stars - 1)
    for verdict in feedback or []:
        if verdict == "buy": stars = min(5, stars + 1)
        elif verdict in {"expensive", "toolong", "skip", "badairline"}: stars = max(1, stars - 1)
    stars = max(1, min(5, stars + preference_adjustment(
        flight, filters, preferences, route=route, destination=destination, cabin=cabin
    )))
    return stars


def fetch_existing(monitor_id):
    return fetch_all_rows("user_matches", {
        "monitor_id": "eq." + monitor_id,
        "select": "id,offer_id,notified_at,last_notified_price,min_price_for_user,telegram_eligible,new_airline,feedback,flight_offers(route,origin,destination,travel_date,return_date,trip_type,cabin,airline,airline_name,price_pln)",
        "order": "updated_at.asc,id.asc",
    })


def fetch_preferences(user_id):
    """Read durable preferences or fail closed before rating/sending offers."""
    last_error = None
    for attempt in range(PREFERENCE_FETCH_RETRIES):
        try:
            return fetch_all_rows("user_preference_signals", {
                "user_id": "eq." + user_id,
                "select": "dimension,value,cabin,score",
                "order": "updated_at.desc,dimension.asc,value.asc",
            })
        except Exception as exc:
            last_error = exc
            if attempt + 1 < PREFERENCE_FETCH_RETRIES:
                time.sleep(2 ** attempt)
    raise RuntimeError(
        "Nie udało się bezpiecznie odczytać preferencji użytkownika %s: %s"
        % (user_id, str(last_error)[:160])
    ) from last_error


def save_offer(task, flight):
    trip_type_value = task.get("trip_type") or ("round_trip" if task.get("return_date") else "one_way")
    tags = list(flight.get("tags") or [])
    verified = trip_type_value == "one_way" or bool(flight.get("round_trip_verified", False))
    if trip_type_value == "round_trip" and not verified and "Powrót do potwierdzenia" not in tags:
        tags.append("Powrót do potwierdzenia")
    verification_status = "verified" if verified else "pending_return"
    verification_note = "" if verification_status == "verified" else "Google nie udostępnił jeszcze szczegółów odcinka powrotnego"
    payload = {"fingerprint": offer_fingerprint(task, flight), "source": flight.get("source") or "Google Flights (cena na żywo)", "route": "%s → %s" % (task["origin"], task["dest"]), "origin": task["origin"], "destination": task["dest"], "travel_date": task["date"], "return_date": task.get("return_date"), "trip_type": trip_type_value, "cabin": task["cabin"].upper().replace("-", "_"), "airline": flight.get("airline", ""), "airline_name": flight.get("airline_name", ""), "price_pln": flight.get("price_pln"), "duration_minutes": round((flight.get("duration_h") or 0) * 60) or None, "stops": flight.get("stops"), "departure": flight.get("departure", ""), "aircraft": flight.get("aircraft", ""), "tags": tags, "verification_status": verification_status, "verification_note": verification_note, "link": flight.get("link", ""), "last_seen_at": datetime.utcnow().isoformat() + "Z", "raw": flight}
    try:
        rows = api("POST", "flight_offers", body=payload, params={"on_conflict": "fingerprint"})
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise
        # The quality columns are additive. Until Supabase applies the new
        # migration, keep saving the core offer instead of stopping scans.
        legacy_payload = dict(payload)
        legacy_payload.pop("verification_status", None)
        legacy_payload.pop("verification_note", None)
        rows = api("POST", "flight_offers", body=legacy_payload, params={"on_conflict": "fingerprint"})
    offer = rows[0] if rows else api("GET", "flight_offers", params={"fingerprint": "eq." + payload["fingerprint"], "select": "*"})[0]
    try:
        if flight.get("price_pln"):
            offer_id = offer["id"]
            current_price = int(flight["price_pln"])
            previous_price = PRICE_HISTORY_LAST.get(offer_id)
            if previous_price is None:
                latest = api("GET", "offer_price_history", params={
                    "offer_id": "eq." + offer_id,
                    "select": "price_pln",
                    "order": "observed_at.desc",
                    "limit": "1",
                })
                previous_price = int(latest[0]["price_pln"]) if latest else None
            if previous_price != current_price:
                api("POST", "offer_price_history", body={"offer_id": offer_id, "price_pln": current_price})
            PRICE_HISTORY_LAST[offer_id] = current_price
    except Exception as exc:
        log("Nie udało się zapisać historii ceny: %s" % str(exc)[:120])
    return offer


def alert_text(offer, stars, match_id):
    def stop_label(value):
        try:
            count = int(value)
        except (TypeError, ValueError):
            return "%s przesiad." % html.escape(str(value), quote=True)
        if count == 0:
            return "bez przesiadek"
        if count == 1:
            return "1 przesiadka"
        if count < 5:
            return "%d przesiadki" % count
        return "%d przesiadek" % count

    duration = offer.get("duration_minutes") or 0
    raw = offer.get("raw") if isinstance(offer.get("raw"), dict) else {}
    route = html.escape(str(offer.get("route", "")), quote=True)
    cabin = html.escape(str(offer.get("cabin", "")), quote=True)
    airline = html.escape(str(offer.get("airline_name", "")), quote=True)
    aircraft = html.escape(str(offer.get("aircraft", "")), quote=True)
    travel_date = html.escape(str(offer.get("travel_date", "")), quote=True)
    return_date = html.escape(str(offer.get("return_date", "")), quote=True)
    link = str(offer.get("link", "")) if str(offer.get("link", "")).lower().startswith(("http://", "https://")) else ""
    tags = [str(tag) for tag in (offer.get("tags") or []) if str(tag).strip()]
    tag_line = "\n🏷 " + html.escape(" · ".join(tags), quote=True) if tags else ""
    aircraft_line = "\n🛫 " + aircraft if aircraft else ""
    dates = travel_date + (" → " + return_date if return_date else "")
    if offer.get("return_date") and raw.get("round_trip_verified") and raw.get("return_duration_h") is not None:
        outbound_duration = raw.get("outbound_duration_h") or 0
        return_duration = raw.get("return_duration_h") or 0
        outbound_stops = raw.get("outbound_stops", "?")
        return_stops = raw.get("return_stops", "?")
        travel_quality = "🛫 Tam: %dh %02dm · %s\n↩️ Powrót: %dh %02dm · %s" % (
            int(outbound_duration), round((outbound_duration % 1) * 60), stop_label(outbound_stops),
            int(return_duration), round((return_duration % 1) * 60), stop_label(return_stops),
        )
    else:
        travel_quality = "%dh %02dm · %s" % (duration // 60, duration % 60, stop_label(offer.get("stops", "?")))
    quality_separator = "\n" if "\n" in travel_quality else " · "
    return ("<b>%s</b>\n🧭 <b>%s</b> · %s\n✈️ %s%s\n💰 <b>%s PLN</b>\n🗓 %s%s%s\n🔗 <a href=\"%s\">Otwórz ofertę</a>" % ("⭐" * stars, route, cabin, airline, aircraft_line, f"{offer.get('price_pln') or 0:,}".replace(",", " "), dates, quality_separator, travel_quality + tag_line, html.escape(link, quote=True)))


def airline_identity(flight):
    code = str(flight.get("airline") or "").strip().upper()
    if code:
        return code
    return " ".join(str(flight.get("airline_name") or "").lower().split())


def historical_duplicate(previous, route, cabin, airline, travel_date, price, return_date=None, trip_type_value="one_way"):
    if price is None or not airline:
        return False
    prices = []
    for old in previous:
        offer = old.get("flight_offers") or {}
        old_cabin = str(offer.get("cabin") or "").upper().replace("-", "_")
        old_trip = offer.get("trip_type") or ("round_trip" if offer.get("return_date") else "one_way")
        exact_same_query = offer.get("travel_date") == travel_date and offer.get("return_date") == return_date
        if (offer.get("route") != route or old_cabin != cabin or old_trip != trip_type_value
                or airline_identity(offer) != airline or exact_same_query):
            continue
        for value in (offer.get("price_pln"), old.get("min_price_for_user")):
            if value is not None:
                prices.append(int(value))
    return bool(prices) and price >= min(prices)


def price_drop_eligible(old_price, current_price, drop_percent):
    return old_price is not None and current_price <= old_price * (1 - drop_percent / 100)


def process_link_updates():
    telegram_io.process_link_updates(api, telegram, TG_TOKEN)


def send_due_alert(match, offer, monitor, connection):
    rules = monitor.get("telegram_rules") or {}
    stars = match["stars"]
    if not connection or stars < int(rules.get("min_stars") or 4):
        return False
    if "Powrót do potwierdzenia" in (offer.get("tags") or []):
        return False
    price = offer.get("price_pln") or 0
    budget = int((monitor.get("filters") or {}).get("budget_pln") or 999999)
    if price > budget:
        return False
    old = match.get("last_notified_price")
    drop = float(rules.get("drop_percent") or 10) / 100
    is_drop = price_drop_eligible(old, price, drop * 100)
    is_new_low = bool(match.get("telegram_eligible"))
    is_new_airline = bool(match.get("new_airline"))
    same_offer = bool(match.get("_same_offer"))
    pending_unnotified = bool(match.get("_pending_unnotified"))
    immediate_new_low = bool(rules.get("immediate_new_low", False)) and not same_offer
    if not is_drop and not is_new_airline and not pending_unnotified and not (is_new_low and immediate_new_low):
        return False
    sent = telegram("sendMessage", {"chat_id": connection["chat_id"], "text": alert_text(offer, stars, match["id"]), "parse_mode": "HTML", "disable_web_page_preview": False, "reply_markup": {"inline_keyboard": [[{"text": "👍 Kupiłbym", "callback_data": "fb|%s|buy" % match["id"]}, {"text": "💸 Za drogo", "callback_data": "fb|%s|expensive" % match["id"]}], [{"text": "🙅 Nie interesuje", "callback_data": "fb|%s|skip" % match["id"]}, {"text": "⏱ Za długo", "callback_data": "fb|%s|toolong" % match["id"]}, {"text": "✈️ Zła linia", "callback_data": "fb|%s|badairline" % match["id"]}]]}})
    if not sent or not sent.get("ok"):
        raise RuntimeError("Telegram nie potwierdził wysłania alertu")
    api("PATCH", "user_matches", body={"notified_at": datetime.utcnow().isoformat() + "Z", "last_notified_price": price}, params={"id": "eq." + match["id"]})
    return True


def process_candidate(monitor, task, flight, previous=None, preferences=None, market_prices=None):
    filters = monitor.get("filters") or {}
    if not quality(flight, filters) or not budget_ok(flight, filters):
        return 0, 0
    if previous is None:
        previous = fetch_existing(monitor["id"])
    route = "%s → %s" % (task["origin"], task["dest"])
    cabin = str(task.get("cabin") or "").upper().replace("-", "_")
    return_date = task.get("return_date")
    trip_type_value = task.get("trip_type") or ("round_trip" if return_date else "one_way")
    airline = airline_identity(flight)
    previous_prices = []
    route_prices = list(market_prices or [])
    matching_feedback = []
    route_airlines = set()
    for old in previous if airline else []:
        old_offer = old.get("flight_offers") or {}
        old_airline = airline_identity(old_offer)
        old_cabin = str(old_offer.get("cabin") or "").upper().replace("-", "_")
        old_trip = old_offer.get("trip_type") or ("round_trip" if old_offer.get("return_date") else "one_way")
        if old_offer.get("route") == route and old_cabin == cabin and old_trip == trip_type_value:
            route_airlines.add(old_airline)
            for value in (old_offer.get("price_pln"), old.get("min_price_for_user")):
                if value is not None:
                    route_prices.append(value)
        if old_offer.get("route") == route and old_cabin == cabin and old_trip == trip_type_value and old_offer.get("return_date") == return_date and old_airline == airline:
            if old.get("feedback"):
                matching_feedback.append(old["feedback"])
            for value in (old_offer.get("price_pln"), old.get("min_price_for_user")):
                if value is not None:
                    previous_prices.append(int(value))
    if historical_duplicate(previous, route, cabin, airline, task["date"], flight.get("price_pln"), return_date=return_date, trip_type_value=trip_type_value):
        # Nowy dzień tej samej trasy i linii nie jest nową okazją, jeśli kosztuje tyle samo lub więcej.
        return 0, 0
    offer = save_offer(task, flight)
    row = next((x for x in previous if x["offer_id"] == offer["id"]), None)
    current_price = flight.get("price_pln")
    previous_min = min(previous_prices) if previous_prices else None
    prices = previous_prices + ([current_price] if current_price is not None else [])
    stars = score(
        flight, filters,
        matching_feedback if preferences is None else None,
        preferences=preferences, route=route,
        destination=task["dest"], cabin=cabin,
        market_prices=route_prices,
    )
    rules = monitor.get("telegram_rules") or {}
    is_new_low = bool(current_price is not None and (previous_min is None or current_price < previous_min))
    is_new_airline = bool(airline and airline not in route_airlines)
    round_trip_verified = "Powrót do potwierdzenia" not in (offer.get("tags") or [])
    match = {"user_id": monitor["user_id"], "monitor_id": monitor["id"], "offer_id": offer["id"], "stars": stars,
             "telegram_eligible": round_trip_verified and (is_new_low or is_new_airline) and stars >= int(rules.get("min_stars") or 4),
             "new_airline": is_new_airline,
             "min_price_for_user": min(prices) if prices else None}
    # Jeżeli alert nie został jeszcze wysłany (brak połączenia Telegram albo
    # chwilowy błąd API), zachowujemy jego kwalifikację na następny przebieg.
    # Po skutecznym wysłaniu notified_at jest ustawione i kwalifikacja może
    # zostać wyliczona od nowa, aby nie wysyłać duplikatów.
    if row and not row.get("notified_at"):
        match["telegram_eligible"] = bool(match["telegram_eligible"] or row.get("telegram_eligible"))
        match["new_airline"] = bool(match["new_airline"] or row.get("new_airline"))
    saved = api("POST", "user_matches", body=match, params={"on_conflict": "user_id,monitor_id,offer_id"})
    current = saved[0] if saved else (row or match)
    current["_same_offer"] = bool(row)
    current["_pending_unnotified"] = bool(row and not row.get("notified_at") and (row.get("telegram_eligible") or match["telegram_eligible"]))
    connection = api("GET", "telegram_connections", params={"user_id": "eq." + monitor["user_id"], "select": "chat_id"})
    sent = int(send_due_alert(current, offer, monitor, connection[0] if connection else None))
    if sent:
        current["notified_at"] = datetime.utcnow().isoformat() + "Z"
        current["last_notified_price"] = offer.get("price_pln")
        current["_pending_unnotified"] = False
    cache_entry = {**current, "flight_offers": offer}
    if row:
        for index, old in enumerate(previous):
            if old.get("offer_id") == offer.get("id"):
                previous[index] = cache_entry
                break
    else:
        previous.append(cache_entry)
    return 1, sent


def main():
    if not SUPABASE_URL or not SERVICE_KEY or not TG_TOKEN:
        raise SystemExit("Brak SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY albo TG_BOT_TOKEN")
    if PROCESS_TELEGRAM_ONLY:
        # Odpowiedzi Telegrama obsługuje wyłącznie dedykowany workflow.
        # Ta gałąź chroni stare ręczne uruchomienia przed rozpoczęciem skanu.
        log("Odbiór Telegrama jest obsługiwany przez telegram_feedback.py")
        return
    mark_stale_offers()
    now = datetime.utcnow()
    active_profiles = api("GET", "profiles", params={"status": "eq.active", "select": "id", "limit": "20"})
    active_ids = [row["id"] for row in active_profiles]
    monitors = api("GET", "monitors", params={"status": "eq.active", "user_id": "in.(%s)" % ",".join(active_ids) if active_ids else "in.(00000000-0000-0000-0000-000000000000)", "select": "*", "limit": "100"})
    active = []
    for monitor in monitors:
        expiry = monitor.get("expires_at") or ""
        if expiry and expiry < date.today().isoformat():
            api("PATCH", "monitors", body={"status": "expired"}, params={"id": "eq." + monitor["id"]})
            continue
        active.append(monitor)
    sync_errors = []
    for monitor in active:
        try:
            sync_monitor_scan_items(monitor)
        except Exception as exc:
            error = "Kolejka monitora %s: %s" % (monitor["id"], str(exc)[:160])
            sync_errors.append(error)
            log("Nie udało się odświeżyć kolejki monitora: %s" % error)
    if FORCE_SCAN:
        force_due_scan_items(active, now)
        log("Ręczny start: wymuszono sprawdzenie kolejki aktywnych monitorów")

    all_due_items = fetch_due_scan_items([m["id"] for m in active], now)
    active_by_id = {m["id"]: m for m in active}
    for item in all_due_items:
        item["user_id"] = active_by_id[item["monitor_id"]]["user_id"]
    limits = adaptive_query_limits()
    due_items = select_scan_items(all_due_items, max_standard=limits["standard"], max_first=limits["first"])
    tasks_by_key = {}
    for item in due_items:
        task = task_from_item(item)
        tasks_by_key.setdefault(task_key(task), {**task, "item_ids": [], "monitor_ids": [], "user_ids": []})
        tasks_by_key[task_key(task)]["item_ids"].extend(task["item_ids"])
        tasks_by_key[task_key(task)]["monitor_ids"].extend(task["monitor_ids"])
        tasks_by_key[task_key(task)]["user_ids"].extend(task["user_ids"])
    all_tasks = list(tasks_by_key.values())
    first_tasks = [task for task in all_tasks if task["cabin"] == "first"][:limits["first"]]
    standard_tasks = [task for task in all_tasks if task["cabin"] != "first"][:limits["standard"]]
    tasks = standard_tasks + first_tasks
    log("Aktywne monitory: %d, zaległe kombinacje: %d, wybrane elementy: %d, zapytania w tym przebiegu: %d, limity: %d + %d First" % (len(active), len(all_due_items), len(due_items), len(tasks), limits["standard"], limits["first"]))
    run = None
    if RESERVED_RUN_ID:
        reserved = api("GET", "scan_runs", params={"id": "eq." + RESERVED_RUN_ID, "select": "*"})
        run = reserved[0] if reserved else None
        if run:
            api("PATCH", "scan_runs", body={"status": "running", "query_count": 0, "standard_limit": limits["standard"], "first_limit": limits["first"], "blocked": False, "error": None}, params={"id": "eq." + RESERVED_RUN_ID})
    if not run:
        run = api("POST", "scan_runs", body={"query_count": 0, "status": "running", "standard_limit": limits["standard"], "first_limit": limits["first"], "blocked": False})[0]
    offers_count = 0
    sent_count = 0
    task_errors = list(sync_errors)
    blocked = False
    executed_count = 0
    source_errors_in_row = 0
    successful_google_tasks = 0
    failed_google_tasks = 0
    source_degraded = False
    source_capacity_reached = False
    runtime_limit_reached = False
    history_cache = {}
    preference_cache = {}
    scan_started_monotonic = time.monotonic()
    try:
        # Preferencje są częścią reguł alertu. Jeżeli baza nie pozwala ich
        # odczytać, kończymy przed pierwszym zapytaniem Google zamiast oceniać
        # i wysyłać oferty z pominięciem wyuczonych decyzji użytkownika.
        for user_id in sorted({monitor["user_id"] for monitor in active}):
            preference_cache[user_id] = fetch_preferences(user_id)
        for task in tasks:
            if time.monotonic() - scan_started_monotonic >= MAX_SCAN_RUNTIME_SECONDS:
                task_errors.append(
                    "Skan zatrzymany po osiągnięciu limitu czasu; pozostałe pozycje wrócą w następnym przebiegu"
                )
                runtime_limit_reached = True
                log(task_errors[-1])
                break
            executed_count += 1
            task_failed = False
            try:
                _level, flights = fetch_task(task)
                source_errors_in_row = 0
            except gflights.BlockedError as exc:
                # Jedna twarda blokada zatrzymuje cały kolektor Google. Kolejne
                # pozycje pozostają zaległe i wrócą w następnym przebiegu.
                task_errors.append("%s-%s-%s: %s" % (task["origin"], task["dest"], task["date"], str(exc)[:120]))
                log("Przerwano Google po wykryciu blokady: %s" % task_errors[-1])
                blocked = True
                break
            except gflights.SourceCapacityError as exc:
                task_errors.append("Google: %s; pozostałe kombinacje zachowano na kolejny skan" % str(exc)[:180])
                log(task_errors[-1])
                source_capacity_reached = True
                break
            except Exception as exc:
                task_errors.append("%s-%s-%s: %s" % (task["origin"], task["dest"], task["date"], str(exc)[:120]))
                log("Pominięto zapytanie po błędzie źródła: %s" % task_errors[-1])
                source_errors_in_row += 1
                failed_google_tasks += 1
                task_failed = True
            else:
                successful_google_tasks += 1
                related = [m for m in active if m["id"] in task["monitor_ids"]]
                preferred_codes = set(PRIORITY)
                for monitor in related:
                    for airline in (monitor.get("filters") or {}).get("preferred_airlines", []):
                        code = gflights.airline_code(str(airline))
                        if code:
                            preferred_codes.add(code)
                for flight in gflights.cheapest_picks(flights, preferred_codes, max_options=3):
                    for monitor in related:
                        try:
                            if monitor["id"] not in history_cache:
                                history_cache[monitor["id"]] = fetch_existing(monitor["id"])
                            user_id = monitor["user_id"]
                            added, sent = process_candidate(
                                monitor, task, flight, history_cache[monitor["id"]],
                                preference_cache[user_id],
                                market_prices=[item.get("price_pln") for item in flights],
                            )
                            offers_count += added; sent_count += sent
                        except Exception as exc:
                            task_errors.append("%s-%s-%s/%s: %s" % (task["origin"], task["dest"], task["date"], monitor["id"], str(exc)[:120]))
                            log("Pominięto ofertę po błędzie zapisu/alertu: %s" % task_errors[-1])
            finally:
                # Keep the inter-request throttle active after parse/network
                # errors too. A malformed Google response must not turn into
                # a burst of unthrottled requests.
                if not blocked:
                    time.sleep(REQUEST_DELAY_SECONDS)
            if task_failed:
                retry_at = now + timedelta(hours=1)
                api("PATCH", "monitor_scan_items", body={"next_scan_at": retry_at.isoformat() + "Z"}, params={"id": "in.(%s)" % ",".join(task["item_ids"])})
                if source_errors_in_row >= 3:
                    task_errors.append("Google: obwód ochronny po trzech kolejnych błędach źródła")
                    source_degraded = True
                    break
            else:
                api("PATCH", "monitor_scan_items", body={"last_scanned_at": now.isoformat() + "Z", "next_scan_at": (now + timedelta(hours=SCAN_INTERVAL_HOURS)).isoformat() + "Z"}, params={"id": "in.(%s)" % ",".join(task["item_ids"])})
                api("PATCH", "monitors", body={"last_scanned_at": now.isoformat() + "Z", "next_scan_at": (now + timedelta(hours=SCAN_INTERVAL_HOURS)).isoformat() + "Z"}, params={"id": "in.(%s)" % ",".join(task["monitor_ids"])})
            if executed_count % PROGRESS_UPDATE_EVERY == 0:
                update_scan_progress(run["id"], executed_count, offers_count)
        # RSS nie zużywa limitu zapytań Google, ale przechodzi ten sam filtr
        # dat, klasy, trasy i osobnych reguł Telegrama.
        rss_active = [m for m in active if trip_type(m.get("filters") or {}) == "one_way"]
        for monitor, flight, origin, dest, travel_date in rss.candidates(rss_active):
            task = {"origin": origin, "dest": dest, "date": travel_date,
                    "cabin": str(flight.get("cabin") or "BUSINESS").lower().replace("_", "-")}
            try:
                if monitor["id"] not in history_cache:
                    history_cache[monitor["id"]] = fetch_existing(monitor["id"])
                user_id = monitor["user_id"]
                added, sent = process_candidate(
                    monitor, task, flight, history_cache[monitor["id"]],
                    preference_cache[user_id],
                )
                offers_count += added; sent_count += sent
            except Exception as exc:
                task_errors.append("RSS-%s-%s-%s/%s: %s" % (origin, dest, travel_date, monitor["id"], str(exc)[:120]))
                log("Pominięto ofertę RSS po błędzie: %s" % task_errors[-1])
        source_unavailable = executed_count > 0 and failed_google_tasks > 0 and successful_google_tasks == 0
        final_status = "blocked" if blocked else ("error" if source_degraded or source_unavailable else ("partial" if task_errors or source_capacity_reached or runtime_limit_reached else "ok"))
        api("PATCH", "scan_runs", body={"finished_at": datetime.utcnow().isoformat() + "Z", "query_count": executed_count, "offer_count": offers_count, "status": final_status, "blocked": blocked, "error": " | ".join(task_errors)[:500] or None}, params={"id": "eq." + run["id"]})
        log("Oferty: %d, alerty Telegram: %d" % (offers_count, sent_count))
        if blocked:
            raise ScanBlockedRun("Google zablokował skan; kolejny przebieg pozostaje na bezpiecznym limicie")
        if source_degraded or source_unavailable:
            raise ScanSourceRun("Google zmienił lub zwrócił uszkodzoną strukturę danych; skan został bezpiecznie zatrzymany")
    except ScanBlockedRun:
        raise
    except ScanSourceRun:
        raise
    except Exception as exc:
        api("PATCH", "scan_runs", body={"finished_at": datetime.utcnow().isoformat() + "Z", "query_count": executed_count, "offer_count": offers_count, "status": "error", "error": str(exc)[:500]}, params={"id": "eq." + run["id"]})
        raise


if __name__ == "__main__":
    main()
