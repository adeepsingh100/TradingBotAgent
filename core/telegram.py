"""Raw Telegram Bot API call -- no SDK, it's one HTTP POST. See
core/alerts.py for the toggle/cooldown logic that decides whether to
call this at all."""

from __future__ import annotations

import requests

from core.config import settings


def send_telegram_message(text: str) -> bool:
    if not settings.telegram_bot_token or not settings.telegram_chat_id:
        return False
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{settings.telegram_bot_token}/sendMessage",
            json={"chat_id": settings.telegram_chat_id, "text": text},
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except requests.RequestException:
        return False
