#!/usr/bin/env python3
"""Wspólny skaner Flight Radar by Kyudo: stan i dane są wyłącznie w Supabase."""
import hashlib
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta

import gflights
import rss

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
MAX_STANDARD = 30
MAX_FIRST = 2
PRIORITY = {"QR", "EY", "EK", "WY", "TK", "BR", "SQ", "CX", "NH", "JL"}


def log(text):
    print("[%s] %s" % (datetime.utcnow().isoformat(timespec="seconds"), text))


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


def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def parse_date(value):
    return datetime.strptime(value, "%Y-%m-%d").date()


def date_range(filters):
    start = parse_date(filters.get("from"))
    end = parse_date(filters.get("to") or filters.get("from"))
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def expand_one_monitor(monitor, tick):
    f = monitor.get("filters") or {}
    origins = [x.upper() for x in f.get("origins", [])]
    destinations = [x.upper() for x in f.get("destinations", [])]
    dates = date_range(f)
    cabin = (f.get("cabin") or "BUSINESS").lower().replace("_", "-")
    if not origins or not destinations or not dates:
        return None
    combinations = [(o, d, day) for o in origins for d in destinations for day in dates]
    index = (tick + int(hashlib.sha1(monitor["id"].encode()).hexdigest()[:8], 16)) % len(combinations)
    origin, dest, day = combinations[index]
    return {"origin": origin, "dest": dest, "date": day.isoformat(), "cabin": cabin,
            "monitor_ids": [monitor["id"]], "user_ids": [monitor["user_id"]]}


def task_key(task):
    return "%s|%s|%s|%s" % (task["origin"], task["dest"], task["date"], task["cabin"])


def offer_fingerprint(task, flight):
    raw = "%s|%s|%s|%s|%s|%s|%s" % (task["origin"], task["dest"], task["date"], task["cabin"], flight.get("airline", ""), flight.get("departure", ""), flight.get("duration_h", ""))
    return hashlib.sha1(raw.encode()).hexdigest()


def quality(flight, filters):
    duration = flight.get("duration_h")
    if duration and duration > float(filters.get("max_duration_h") or 99):
        return False
    if flight.get("stops") is not None and filters.get("max_stops") is not None and flight["stops"] > int(filters["max_stops"]):
        return False
    if filters.get("direct_only") and flight.get("stops") != 0:
        return False
    excluded = [str(x).lower() for x in filters.get("excluded_airlines", [])]
    return not any(x in (flight.get("airline_name") or "").lower() for x in excluded)


def score(flight, filters):
    price = flight.get("price_pln") or 999999
    budget = int(filters.get("budget_pln") or 999999)
    stars = 5 if price <= budget * .55 else 4 if price <= budget * .75 else 3 if price <= budget else 2 if price <= budget * 1.15 else 1
    if flight.get("airline") in PRIORITY:
        stars = min(5, stars + 1)
    if flight.get("duration_h") and flight["duration_h"] > 20:
        stars = max(1, stars - 1)
    return stars


def fetch_existing(monitor_id):
    return api("GET", "user_matches", params={"monitor_id": "eq." + monitor_id, "select": "id,offer_id,notified_at,last_notified_price,min_price_for_user,flight_offers(route,origin,destination,travel_date,airline,airline_name,price_pln)"})


def save_offer(task, flight):
    payload = {"fingerprint": offer_fingerprint(task, flight), "source": flight.get("source") or "Google Flights (cena na żywo)", "route": "%s → %s" % (task["origin"], task["dest"]), "origin": task["origin"], "destination": task["dest"], "travel_date": task["date"], "cabin": task["cabin"].upper(), "airline": flight.get("airline", ""), "airline_name": flight.get("airline_name", ""), "price_pln": flight.get("price_pln"), "duration_minutes": round((flight.get("duration_h") or 0) * 60) or None, "stops": flight.get("stops"), "departure": flight.get("departure", ""), "tags": flight.get("tags", []), "link": flight.get("link", ""), "last_seen_at": datetime.utcnow().isoformat() + "Z", "raw": flight}
    rows = api("POST", "flight_offers", body=payload, params={"on_conflict": "fingerprint"})
    return rows[0] if rows else api("GET", "flight_offers", params={"fingerprint": "eq." + payload["fingerprint"], "select": "*"})[0]


def alert_text(offer, stars, match_id):
    duration = offer.get("duration_minutes") or 0
    return ("<b>%s</b>\n🧭 <b>%s</b> · %s\n✈️ %s\n💰 <b>%s PLN</b>\n🗓 %s · %dh %02dm · %s przes.\n🔗 <a href=\"%s\">Otwórz ofertę</a>" % ("⭐" * stars, offer["route"], offer["cabin"], offer.get("airline_name", ""), f"{offer.get('price_pln') or 0:,}".replace(",", " "), offer["travel_date"], duration // 60, duration % 60, offer.get("stops", "?"), offer.get("link", "")))


def process_link_updates():
    if not TG_TOKEN:
        return
    state = api("GET", "telegram_state", params={"id": "eq.1", "select": "update_offset"})
    offset = int(state[0]["update_offset"]) if state else 0
    response = telegram("getUpdates", {"offset": offset + 1, "timeout": 0}) or {}
    max_id = offset
    for update in response.get("result", []):
        max_id = max(max_id, update["update_id"])
        message = update.get("message") or {}
        callback = update.get("callback_query") or {}
        if callback:
            data = callback.get("data", "")
            if data.startswith("fb|"):
                parts = data.split("|", 2)
                if len(parts) == 3:
                    chat_id = str((callback.get("message") or {}).get("chat", {}).get("id", ""))
                    connections = api("GET", "telegram_connections", params={"chat_id": "eq." + chat_id, "select": "user_id"})
                    if connections:
                        match_id, verdict = parts[1], parts[2]
                        matches = api("GET", "user_matches", params={"id": "eq." + match_id, "user_id": "eq." + connections[0]["user_id"], "select": "id"})
                        if matches:
                            api("POST", "feedback", body={"user_id": connections[0]["user_id"], "match_id": match_id, "verdict": verdict})
                            api("PATCH", "user_matches", body={"feedback": verdict}, params={"id": "eq." + match_id})
                    telegram("answerCallbackQuery", {"callback_query_id": callback.get("id"), "text": "Zapisano"})
            continue
        text = message.get("text", "")
        if not text.startswith("/start "):
            continue
        raw = text.split(" ", 1)[1].strip()
        tokens = api("GET", "telegram_link_tokens", params={"token_hash": "eq." + token_hash(raw), "used_at": "is.null", "expires_at": "gt." + datetime.utcnow().isoformat() + "Z", "select": "user_id"})
        if not tokens:
            continue
        chat = message.get("chat", {})
        chat_id = str(chat.get("id"))
        existing_chat = api("GET", "telegram_connections", params={"chat_id": "eq." + chat_id, "select": "user_id"})
        if existing_chat and existing_chat[0]["user_id"] != tokens[0]["user_id"]:
            telegram("sendMessage", {"chat_id": chat.get("id"), "text": "⚠️ Ten Telegram jest już połączony z innym kontem Flight Radar by Kyudo."})
            continue
        api("POST", "telegram_connections", body={"user_id": tokens[0]["user_id"], "chat_id": chat_id, "username": chat.get("username", "")}, params={"on_conflict": "user_id"})
        api("PATCH", "telegram_link_tokens", body={"used_at": datetime.utcnow().isoformat() + "Z"}, params={"token_hash": "eq." + token_hash(raw)})
        telegram("sendMessage", {"chat_id": chat.get("id"), "text": "✅ Flight Radar by Kyudo połączony. Alerty będą trafiać tutaj."})
    api("PATCH", "telegram_state", body={"update_offset": max_id}, params={"id": "eq.1"})


def send_due_alert(match, offer, monitor, connection):
    rules = monitor.get("telegram_rules") or {}
    stars = match["stars"]
    if not connection or stars < int(rules.get("min_stars") or 4):
        return False
    price = offer.get("price_pln") or 0
    budget = int((monitor.get("filters") or {}).get("budget_pln") or 999999)
    if price > budget and not (set(offer.get("tags") or []) & {"Error Fare", "Mistake Fare"}):
        return False
    old = match.get("last_notified_price")
    drop = float(rules.get("drop_percent") or 10) / 100
    is_new = not match.get("notified_at")
    is_drop = old and price < old * (1 - drop)
    if not is_new and not is_drop:
        return False
    telegram("sendMessage", {"chat_id": connection["chat_id"], "text": alert_text(offer, stars, match["id"]), "parse_mode": "HTML", "disable_web_page_preview": False, "reply_markup": {"inline_keyboard": [[{"text": "👍 Kupiłbym", "callback_data": "fb|%s|buy" % match["id"]}, {"text": "💸 Za drogo", "callback_data": "fb|%s|expensive" % match["id"]}], [{"text": "🙅 Nie interesuje", "callback_data": "fb|%s|skip" % match["id"]}, {"text": "⏱ Za długo", "callback_data": "fb|%s|toolong" % match["id"]}, {"text": "✈️ Zła linia", "callback_data": "fb|%s|badairline" % match["id"]}]]}})
    api("PATCH", "user_matches", body={"notified_at": datetime.utcnow().isoformat() + "Z", "last_notified_price": price}, params={"id": "eq." + match["id"]})
    return True


def process_candidate(monitor, task, flight):
    if not quality(flight, monitor.get("filters") or {}):
        return 0, 0
    previous = fetch_existing(monitor["id"])
    route = "%s → %s" % (task["origin"], task["dest"])
    airline = (flight.get("airline") or flight.get("airline_name") or "").lower()
    for old in previous if airline else []:
        old_offer = old.get("flight_offers") or {}
        old_airline = (old_offer.get("airline") or old_offer.get("airline_name") or "").lower()
        old_price = old_offer.get("price_pln") or old.get("min_price_for_user")
        if (old_offer.get("route") == route and old_airline == airline and old_offer.get("travel_date") != task["date"]
                and old_price is not None and flight.get("price_pln") is not None and flight["price_pln"] >= old_price):
            # Nowy dzień tej samej trasy i linii nie jest nową okazją, jeśli kosztuje tyle samo lub więcej.
            return 0, 0
    offer = save_offer(task, flight)
    row = next((x for x in previous if x["offer_id"] == offer["id"]), None)
    prices = [x.get("min_price_for_user") for x in previous if x.get("min_price_for_user")]
    prices.append(flight.get("price_pln"))
    match = {"user_id": monitor["user_id"], "monitor_id": monitor["id"], "offer_id": offer["id"], "stars": score(flight, monitor.get("filters") or {}), "min_price_for_user": min(prices)}
    saved = api("POST", "user_matches", body=match, params={"on_conflict": "user_id,monitor_id,offer_id"})
    current = saved[0] if saved else (row or match)
    connection = api("GET", "telegram_connections", params={"user_id": "eq." + monitor["user_id"], "select": "chat_id"})
    return 1, int(send_due_alert(current, offer, monitor, connection[0] if connection else None))


def main():
    if not SUPABASE_URL or not SERVICE_KEY:
        raise SystemExit("Brak sekretów Supabase")
    process_link_updates()
    now = datetime.utcnow()
    tick = int(now.timestamp() // (3 * 3600))
    monitors = api("GET", "monitors", params={"status": "eq.active", "select": "*", "limit": "100"})
    active = []
    for monitor in monitors:
        expiry = monitor.get("expires_at") or ""
        if expiry and expiry < date.today().isoformat():
            api("PATCH", "monitors", body={"status": "expired"}, params={"id": "eq." + monitor["id"]})
            continue
        active.append(monitor)
    # Najdawniej sprawdzane monitory trafiają na początek kolejki, więc przy
    # ograniczeniu liczby zapytań jedna osoba nie może stale wypierać innych.
    active.sort(key=lambda item: (item.get("last_scanned_at") or "", item.get("created_at") or ""))
    tasks_by_key = {}
    for monitor in active:
        task = expand_one_monitor(monitor, tick)
        if task:
            tasks_by_key.setdefault(task_key(task), {**task, "monitor_ids": [], "user_ids": []})
            tasks_by_key[task_key(task)]["monitor_ids"].append(monitor["id"])
            tasks_by_key[task_key(task)]["user_ids"].append(monitor["user_id"])
    all_tasks = list(tasks_by_key.values())
    first_tasks = [task for task in all_tasks if task["cabin"] == "first"][:MAX_FIRST]
    standard_tasks = [task for task in all_tasks if task["cabin"] != "first"][:MAX_STANDARD]
    tasks = standard_tasks + first_tasks
    log("Aktywne monitory: %d, unikalne zapytania: %d" % (len(active), len(tasks)))
    run = api("POST", "scan_runs", body={"query_count": len(tasks), "status": "running"})[0]
    offers_count = 0
    sent_count = 0
    task_errors = []
    try:
        for task in tasks:
            try:
                _level, flights = gflights.fetch_gf(task["origin"], task["dest"], task["date"], seat=task["cabin"])
            except Exception as exc:
                task_errors.append("%s-%s-%s: %s" % (task["origin"], task["dest"], task["date"], str(exc)[:120]))
                log("Pominięto zapytanie po błędzie źródła: %s" % task_errors[-1])
                continue
            for flight in gflights.cheapest_picks(flights, PRIORITY, max_options=3):
                related = [m for m in active if m["id"] in task["monitor_ids"]]
                for monitor in related:
                    added, sent = process_candidate(monitor, task, flight)
                    offers_count += added; sent_count += sent
            time.sleep(2.0)
            api("PATCH", "monitors", body={"last_scanned_at": now.isoformat() + "Z", "next_scan_at": (now + timedelta(hours=3)).isoformat() + "Z"}, params={"id": "in.(%s)" % ",".join(task["monitor_ids"])})
        # RSS nie zużywa limitu zapytań Google, ale przechodzi ten sam filtr
        # dat, klasy, trasy i osobnych reguł Telegrama.
        for monitor, flight, origin, dest, travel_date in rss.candidates(active):
            task = {"origin": origin, "dest": dest, "date": travel_date, "cabin": (monitor.get("filters") or {}).get("cabin", "BUSINESS")}
            added, sent = process_candidate(monitor, task, flight)
            offers_count += added; sent_count += sent
        api("PATCH", "scan_runs", body={"finished_at": datetime.utcnow().isoformat() + "Z", "offer_count": offers_count, "status": "partial" if task_errors else "ok", "error": " | ".join(task_errors)[:500] or None}, params={"id": "eq." + run["id"]})
        log("Oferty: %d, alerty Telegram: %d" % (offers_count, sent_count))
    except Exception as exc:
        api("PATCH", "scan_runs", body={"finished_at": datetime.utcnow().isoformat() + "Z", "offer_count": offers_count, "status": "error", "error": str(exc)[:500]}, params={"id": "eq." + run["id"]})
        raise


if __name__ == "__main__":
    main()
