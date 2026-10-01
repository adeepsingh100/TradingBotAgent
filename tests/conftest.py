"""Shared test helpers -- not a DB/network fixture (this repo has none
of those in its suite): a convenience for building the candle dicts
core/coindcx/client.py::get_candles returns (verified live field
names: open/high/low/close/volume/time), and a tiny in-memory fake
standing in for just enough of SQLAlchemy's Session (add/flush/delete/
query().filter_by()) for any module that only ever does those."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone


class _FakeQuery:
    def __init__(self, items):
        self._items = list(items)

    def filter_by(self, **kw):
        self._items = [o for o in self._items if all(getattr(o, k) == v for k, v in kw.items())]
        return self

    def order_by(self, *a):
        return self

    def first(self):
        return self._items[0] if self._items else None

    def one_or_none(self):
        return self._items[0] if self._items else None

    def one(self):
        if len(self._items) != 1:
            raise ValueError(f"expected exactly one row, got {len(self._items)}")
        return self._items[0]

    def all(self):
        return list(self._items)


class FakeSession:
    def __init__(self):
        self.added = []
        self.deleted = []

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "executed_at") and obj.executed_at is None:
            obj.executed_at = datetime.now(timezone.utc)
        self.added.append(obj)

    def flush(self):
        pass

    def delete(self, obj):
        self.deleted.append(obj)
        if obj in self.added:
            self.added.remove(obj)

    def query(self, model):
        return _FakeQuery(o for o in self.added if isinstance(o, model))


def make_candles(closes: list[float], volumes: list[float] | None = None) -> list[dict]:
    volumes = volumes or [1.0] * len(closes)
    candles = []
    prev_close = closes[0]
    for i, (close, volume) in enumerate(zip(closes, volumes)):
        candles.append({
            "open": prev_close,
            "high": max(prev_close, close) + 0.01,
            "low": min(prev_close, close) - 0.01,
            "close": close,
            "volume": volume,
            "time": i,
        })
        prev_close = close
    return candles
