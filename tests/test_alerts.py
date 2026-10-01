"""Mocked HTTP (via core.telegram) and a tiny in-memory fake session --
no real network/DB, matching repo convention."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import requests

from core.alerts import maybe_send_alert
from core.db.models import AlertSent


class _FakeQuery:
    def __init__(self, items):
        self._items = list(items)

    def filter_by(self, **kw):
        self._items = [o for o in self._items if all(getattr(o, k) == v for k, v in kw.items())]
        return self

    def all(self):
        return list(self._items)


class FakeSession:
    def __init__(self):
        self.added = []

    def query(self, model):
        return _FakeQuery(o for o in self.added if isinstance(o, model))

    def add(self, obj):
        self.added.append(obj)


def _fake_post_ok(monkeypatch):
    monkeypatch.setattr(requests, "post", lambda *a, **kw: type("R", (), {"raise_for_status": lambda self: None})())


def test_alert_skipped_when_toggle_is_off(monkeypatch):
    _fake_post_ok(monkeypatch)
    session = FakeSession()

    sent = maybe_send_alert(session, "trade_executed", {"telegram_alerts": {"trade_executed": False}}, "hi")

    assert sent is False
    assert session.added == []


def test_event_alert_always_sends_even_when_called_repeatedly(monkeypatch):
    _fake_post_ok(monkeypatch)
    monkeypatch.setattr("core.config.settings.telegram_bot_token", "tok")
    monkeypatch.setattr("core.config.settings.telegram_chat_id", "chat")
    session = FakeSession()

    maybe_send_alert(session, "trade_executed", {"telegram_alerts": {}}, "trade 1")
    maybe_send_alert(session, "trade_executed", {"telegram_alerts": {}}, "trade 2")

    assert len(session.added) == 2  # not a stateful alert type -- never suppressed


def test_stateful_alert_suppressed_within_cooldown_window(monkeypatch):
    _fake_post_ok(monkeypatch)
    monkeypatch.setattr("core.config.settings.telegram_bot_token", "tok")
    monkeypatch.setattr("core.config.settings.telegram_chat_id", "chat")
    session = FakeSession()
    session.add(AlertSent(wallet_id="w1", alert_type="agent_died", sent_at=datetime.now(timezone.utc)))

    sent = maybe_send_alert(session, "agent_died", {"telegram_alerts": {}}, "wallet died", wallet_id="w1")

    assert sent is False
    assert len(session.added) == 1  # no new row added -- suppressed before ever calling telegram


def test_stateful_alert_resends_once_cooldown_has_passed(monkeypatch):
    _fake_post_ok(monkeypatch)
    monkeypatch.setattr("core.config.settings.telegram_bot_token", "tok")
    monkeypatch.setattr("core.config.settings.telegram_chat_id", "chat")
    session = FakeSession()
    stale = datetime.now(timezone.utc) - timedelta(hours=25)
    session.add(AlertSent(wallet_id="w1", alert_type="agent_died", sent_at=stale))

    sent = maybe_send_alert(session, "agent_died", {"telegram_alerts": {}}, "wallet died again", wallet_id="w1")

    assert sent is True
    assert len(session.added) == 2
