#!/usr/bin/env python3
"""Fast scheduled receiver for Telegram feedback buttons."""

import telegram_io


def main():
    webhook = telegram_io.telegram("getWebhookInfo", {}) or {}
    info = webhook.get("result") or {}
    if info.get("url"):
        # Telegram keeps the most recent delivery error in getWebhookInfo even
        # after a webhook recovers. Treat it as active only while updates are
        # still queued; otherwise an old transient error would fail every
        # health check forever.
        pending_updates = int(info.get("pending_update_count") or 0)
        if info.get("last_error_date") and pending_updates > 0:
            raise RuntimeError("Telegram webhook zgłasza błąd dostarczenia: %s" % (info.get("last_error_message") or "unknown"))
        print("Telegram feedback webhook: OK")
        return
    telegram_io.process_link_updates()
    print("Telegram feedback polling fallback: OK")


if __name__ == "__main__":
    main()
