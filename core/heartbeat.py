"""One row per named source, upserted every tick -- see
core/db/models.py::Heartbeat's docstring."""

from __future__ import annotations

from datetime import datetime, timezone

from core.db.models import Heartbeat


def beat(session, source: str, detail: dict | None = None) -> None:
    now = datetime.now(timezone.utc)
    row = session.query(Heartbeat).filter_by(source=source).one_or_none()
    if row is None:
        session.add(Heartbeat(source=source, last_beat_at=now, detail=detail or {}))
    else:
        row.last_beat_at = now
        row.detail = detail or {}
