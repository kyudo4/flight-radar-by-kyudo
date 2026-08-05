"""Durable Telegram alert delivery independent from Google Flights scans."""

import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timedelta


SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
TG_TOKEN = os.environ.get("TG_BOT_TOKEN", "")
MAX_ATTEMPTS = 8


def api(method, path, body=None, params=None):
    if not SUPABASE_URL or not SERVICE_KEY:
        raise RuntimeError("Brak SUPABASE_URL albo SUPABASE_SERVICE_ROLE_KEY")
    url = SUPABASE_URL + "/rest/v1/" + path
    query_params = {key: value for key, value in (params or {}).items() if not str(key).startswith("_")}
    if query_params:
        url += "?" + urllib.parse.urlencode(query_params, doseq=True)
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("apikey", SERVICE_KEY)
    request.add_header("Authorization", "Bearer " + SERVICE_KEY)
    request.add_header("Content-Type", "application/json")
    request.add_header("Accept", "application/json")
    if body is not None:
        preference = "return=representation"
        if params and "on_conflict" in params:
            resolution = params.get("_resolution", "merge-duplicates")
            preference = "resolution=%s,return=representation" % resolution
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


def retry_time(attempts):
    # 5m, 15m, 1h, 3h, then capped at 6h.
    delay_minutes = min(360, 5 * (3 ** max(0, attempts - 1)))
    return datetime.utcnow() + timedelta(minutes=delay_minutes)


def _finish(row, *, status, error=None):
    body = {"status": status, "last_error": error, "updated_at": datetime.utcnow().isoformat() + "Z"}
    if status == "sent":
        body["sent_at"] = datetime.utcnow().isoformat() + "Z"
    elif status in {"retry", "dead"}:
        body["available_at"] = retry_time(int(row.get("attempts") or 1)).isoformat() + "Z"
    api("PATCH", "telegram_outbox", body=body,
        params={"id": "eq." + row["id"], "status": "eq.sending"})


def deliver_pending(limit=50):
    """Claim and deliver a bounded batch. Returns aggregate delivery counts."""
    rows = api("POST", "rpc/claim_telegram_outbox", body={"p_limit": limit})
    result = {"claimed": len(rows), "sent": 0, "retried": 0, "dead": 0}
    for row in rows:
        try:
            response = telegram("sendMessage", {
                "chat_id": row["chat_id"],
                "text": row["message_text"],
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
                "reply_markup": row.get("reply_markup") or {},
            })
            if not response or not response.get("ok"):
                raise RuntimeError(str((response or {}).get("description") or "Telegram nie potwierdził wysłania"))
            completed = api("POST", "rpc/complete_telegram_outbox",
                            body={"p_outbox_id": row["id"]})
            if completed is not True:
                raise RuntimeError("Nie udało się atomowo oznaczyć alertu jako wysłanego")
            result["sent"] += 1
        except Exception as exc:
            error = str(exc)[:300]
            attempts = int(row.get("attempts") or 1)
            status = "dead" if attempts >= MAX_ATTEMPTS else "retry"
            _finish(row, status=status, error=error)
            result[status] += 1
    return result


if __name__ == "__main__":
    print(json.dumps(deliver_pending(), ensure_ascii=False))
