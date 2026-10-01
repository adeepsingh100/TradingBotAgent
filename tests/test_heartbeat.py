from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.db.models import Heartbeat
from core.heartbeat import beat


class _FakeQuery:
    def __init__(self, rows):
        self._rows = rows
        self._source = None

    def filter_by(self, **kw):
        self._source = kw.get("source")
        return self

    def one_or_none(self):
        return next((r for r in self._rows if r.source == self._source), None)


class FakeSession:
    def __init__(self):
        self.added = []

    def query(self, model):
        return _FakeQuery(self.added)

    def add(self, obj):
        self.added.append(obj)


def test_beat_creates_a_row_when_none_exists():
    session = FakeSession()
    beat(session, "worker_tick", detail={"wallets": 2})

    assert len(session.added) == 1
    assert session.added[0].source == "worker_tick"
    assert session.added[0].detail == {"wallets": 2}


def test_beat_updates_the_existing_row_in_place():
    session = FakeSession()
    old_beat_at = datetime.now(timezone.utc) - timedelta(hours=1)
    row = Heartbeat(source="worker_tick", detail={"wallets": 1}, last_beat_at=old_beat_at)
    session.added.append(row)

    beat(session, "worker_tick", detail={"wallets": 3})

    assert len(session.added) == 1  # updated, not duplicated
    assert session.added[0].detail == {"wallets": 3}
    assert session.added[0].last_beat_at >= old_beat_at
