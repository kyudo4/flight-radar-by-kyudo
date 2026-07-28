#!/usr/bin/env python3
"""Fast scheduled receiver for Telegram feedback buttons."""

import telegram_io


if __name__ == "__main__":
    telegram_io.process_link_updates()
    print("Telegram feedback: OK")

