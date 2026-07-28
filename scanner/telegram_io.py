"""Minimal Telegram/Supabase runtime independent from Google Flights."""

import json
import os
import urllib.parse
import urllib.request


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")


def api(method, path, body=None, params=None):
    if not SUPABASE_URL or not SERVICE_KEY:
        raise RuntimeError("Brak SUPABASE_URL albo SUPABASE_SERVICE_ROLE_KEY")
    url = SUPABASE_URL + "/rest/v1/" + path
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("apikey", SERVICE_KEY)
    request.add_header("Authorization", "Bearer " + SERVICE_KEY)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    if body is not None:
        preference = "return=representation"
        if params and "on_conflict" in params:
            preference = "resolution=merge-duplicates,return=representation"
        request.add_header("Prefer", preference)
    with urllib.request.urlopen(request, timeout=30) as response:
        raw = response.read()
        return json.loads(raw) if raw else []


def telegram(method, payload):
    if not TG_TOKEN:
        raise RuntimeError("Brak TG_BOT_TOKEN")
    request = urllib.request.Request(
        "https://api.telegram.org/bot%s/%s" % (TG_TOKEN, method),
        data=json.dumps(payload).encode(), method="POST",
    )
    request.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read())


def process_link_updates(api_call=api, telegram_call=telegram, token=None):
    if not (TG_TOKEN if token is None else token):
        return
    state = api_call("GET", "telegram_state", params={"id": "eq.1", "select": "update_offset"})
    offset = int(state[0]["update_offset"]) if state else 0
    response = telegram_call("getUpdates", {"offset": offset + 1, "timeout": 0}) or {}
    max_id = offset
    for update in response.get("result", []):
        max_id = max(max_id, update["update_id"])
        callback = update.get("callback_query") or {}
        if not callback:
            continue
        data = callback.get("data", "")
        if not data.startswith("fb|"):
            continue
        parts = data.split("|", 2)
        if len(parts) != 3:
            continue
        match_id, verdict = parts[1], parts[2]
        callback_id = callback.get("id")
        if verdict not in {"buy", "expensive", "skip", "toolong", "badairline"}:
            telegram_call("answerCallbackQuery", {
                "callback_query_id": callback_id, "text": "Nieprawidłowa odpowiedź"
            })
            continue
        chat_id = str((callback.get("message") or {}).get("chat", {}).get("id", ""))
        connections = api_call("GET", "telegram_connections", params={
            "chat_id": "eq." + chat_id, "select": "user_id"
        })
        saved = False
        if connections:
            user_id = connections[0]["user_id"]
            matches = api_call("GET", "user_matches", params={
                "id": "eq." + match_id, "user_id": "eq." + user_id, "select": "id"
            })
            if matches:
                api_call("POST", "feedback", body={
                    "user_id": user_id, "match_id": match_id, "verdict": verdict
                }, params={"on_conflict": "user_id,match_id"})
                api_call("PATCH", "user_matches", body={"feedback": verdict},
                         params={"id": "eq." + match_id, "user_id": "eq." + user_id})
                saved = True
        telegram_call("answerCallbackQuery", {
            "callback_query_id": callback_id,
            "text": "Zapisano" if saved else "Nie znaleziono powiązanej oferty",
        })
    api_call("PATCH", "telegram_state", body={"update_offset": max_id}, params={"id": "eq.1"})

