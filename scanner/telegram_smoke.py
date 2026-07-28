#!/usr/bin/env python3
"""Manual end-to-end Telegram check for the primary administrator."""

from datetime import datetime, timezone

import telegram_io


def main():
    admins = telegram_io.api("GET", "profiles", params={
        "role": "eq.admin", "status": "eq.active", "select": "id", "limit": "1"
    })
    if not admins:
        raise SystemExit("Brak aktywnego administratora")
    connections = telegram_io.api("GET", "telegram_connections", params={
        "user_id": "eq." + admins[0]["id"], "select": "chat_id", "limit": "1"
    })
    if not connections:
        raise SystemExit("Administrator nie ma połączenia Telegram")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    response = telegram_io.telegram("sendMessage", {
        "chat_id": connections[0]["chat_id"],
        "text": "✅ Flight Radar by Kyudo: test połączenia zakończony poprawnie\n" + stamp,
    })
    if not response or not response.get("ok"):
        raise SystemExit("Telegram nie potwierdził wiadomości testowej")
    print("Telegram smoke test: OK")


if __name__ == "__main__":
    main()
