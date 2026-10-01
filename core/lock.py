"""Tick lock -- `core/db/models.py::Lock`, acquired via a single atomic
conditional UPDATE...RETURNING, never a read-then-write check (two
ticks racing under CockroachDB's SERIALIZABLE isolation would otherwise
both pass a Python-side check and one would die with an unhandled
40001 at commit, instead of the clean "someone else is ticking" result
the worker needs). No Postgres advisory lock -- CockroachDB doesn't
support those; see core/db/session.py's module docstring.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from core.db.models import Lock


def acquire_tick_lock(session, *, holder: str, ttl_seconds: int = 120, name: str = "tick_lock") -> bool:
    now = datetime.now(timezone.utc)
    new_until = now + timedelta(seconds=ttl_seconds)

    row = session.execute(
        text(
            "UPDATE locks SET locked_until = :new_until, holder = :holder "
            "WHERE name = :name AND (locked_until IS NULL OR locked_until < :now) "
            "RETURNING id"
        ),
        {"new_until": new_until, "holder": holder, "name": name, "now": now},
    ).first()
    if row is not None:
        return True

    existing = session.query(Lock).filter_by(name=name).one_or_none()
    if existing is not None:
        return False  # held by someone else, not yet expired

    # No row at all yet -- bootstrap it (scripts/seed_wallets.py doesn't
    # create this row). A second process racing this same bootstrap loses
    # cleanly: its INSERT hits the table's unique `name` constraint.
    try:
        session.add(Lock(name=name, locked_until=new_until, holder=holder))
        session.flush()
        return True
    except Exception:
        session.rollback()
        return False
