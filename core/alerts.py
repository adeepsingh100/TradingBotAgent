"""Gates every Telegram alert on the `settings` table's `telegram_alerts`
toggle, then -- for alert types that represent ongoing STATE rather than
a one-off event -- suppresses a repeat within its cooldown window using
`alerts_sent` (core/db/models.py::AlertSent's docstring). Event alerts
(trade_executed, strategy_ready_for_live, daily_summary) aren't in the
cooldown map: every call site for those represents a genuinely new
occurrence, so always send.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.db.models import AlertSent
from core.telegram import send_telegram_message

_STATEFUL_COOLDOWN_MINUTES = {
    "heartbeat_missing": 60,
    "agent_died": 24 * 60,
    "daily_loss_limit_hit": 24 * 60,
    "cooldown_triggered": 24 * 60,
    "repeated_llm_failures": 60,
}


def maybe_send_alert(session, alert_type: str, settings_map: dict, text: str, *, wallet_id=None) -> bool:
    toggles = settings_map.get("telegram_alerts", {})
    if not toggles.get(alert_type, True):
        return False

    cooldown_minutes = _STATEFUL_COOLDOWN_MINUTES.get(alert_type)
    if cooldown_minutes:
        cutoff = datetime.now(timezone.utc) - timedelta(minutes=cooldown_minutes)
        recent = [
            a
            for a in session.query(AlertSent).filter_by(alert_type=alert_type, wallet_id=wallet_id).all()
            if a.sent_at >= cutoff
        ]
        if recent:
            return False

    sent = send_telegram_message(text)
    session.add(AlertSent(wallet_id=wallet_id, alert_type=alert_type, payload={"text": text, "sent": sent}))
    return sent
