"""No real DB -- a tiny fake standing in for just enough of SQLAlchemy's
Session to exercise the atomic-UPDATE CAS in core/lock.py (see that
module's docstring for why this has to be one statement, not a
read-then-write check)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.db.models import Lock
from core.lock import acquire_tick_lock


class _FakeResult:
    def __init__(self, row):
        self._row = row

    def first(self):
        return self._row


class _FakeQuery:
    def __init__(self, rows: dict[str, Lock]):
        self._rows = rows
        self._name = None

    def filter_by(self, **kw):
        self._name = kw.get("name")
        return self

    def one_or_none(self):
        return self._rows.get(self._name)


class FakeLockSession:
    def __init__(self, existing: Lock | None = None):
        self.rows: dict[str, Lock] = {}
        if existing is not None:
            self.rows[existing.name] = existing

    def execute(self, stmt, params):
        row = self.rows.get(params["name"])
        if row is None:
            return _FakeResult(None)
        if row.locked_until is None or row.locked_until < params["now"]:
            row.locked_until = params["new_until"]
            row.holder = params["holder"]
            return _FakeResult((row,))
        return _FakeResult(None)

    def query(self, model):
        return _FakeQuery(self.rows)

    def add(self, obj):
        self.rows[obj.name] = obj

    def flush(self):
        pass

    def rollback(self):
        pass


def test_acquire_tick_lock_bootstraps_when_no_row_exists():
    session = FakeLockSession(existing=None)
    assert acquire_tick_lock(session, holder="worker-1") is True
    assert session.rows["tick_lock"].holder == "worker-1"


def test_acquire_tick_lock_succeeds_when_expired():
    now = datetime.now(timezone.utc)
    existing = Lock(name="tick_lock", locked_until=now - timedelta(seconds=10), holder="old-holder")
    session = FakeLockSession(existing=existing)

    assert acquire_tick_lock(session, holder="worker-2") is True
    assert session.rows["tick_lock"].holder == "worker-2"


def test_acquire_tick_lock_fails_when_held_and_not_expired():
    now = datetime.now(timezone.utc)
    existing = Lock(name="tick_lock", locked_until=now + timedelta(seconds=60), holder="other-worker")
    session = FakeLockSession(existing=existing)

    assert acquire_tick_lock(session, holder="worker-2") is False
    assert session.rows["tick_lock"].holder == "other-worker"
