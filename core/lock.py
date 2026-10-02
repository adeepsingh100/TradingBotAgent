"""Tick lock -- `core/db/models.py::Lock`, acquired via a single atomic
conditional UPDATE...RETURNING, never a read-then-write check (two
ticks racing under CockroachDB's SERIALIZABLE isolation would otherwise
both pass a Python-side check and one would die with an unhandled
40001 at commit, instead of the clean "someone else is ticking" result
the worker needs). No Postgres advisory lock -- CockroachDB doesn't
support those; see core/db/session.py's module docstring.

Found live: a second writer doesn't fail on a held lock, it WAITS --
the holder's UPDATE is an uncommitted write intent until the holder's
whole transaction commits, and a tick's transaction spans every LLM call
(can be minutes). With /tick firing every minute that piled blocked
requests up behind one slow tick. `lock_timeout` makes the acquire give
up after a couple of seconds instead (CockroachDB raises
LockNotAvailable, which is "held by someone else" -> False), and is
reset to unlimited afterwards so the holder's own later statements
aren't affected. `release_lock` frees it the moment a run finishes, so
the TTL only matters if a holder dies mid-run.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import text

from core.db.models import Lock


def acquire_tick_lock(
    session, *, holder: str, ttl_seconds: int = 600, name: str = "tick_lock", wait_seconds: int = 2,
) -> bool:
    now = datetime.now(timezone.utc)
    new_until = now + timedelta(seconds=ttl_seconds)

    try:
        session.execute(text(f"SET LOCAL lock_timeout = '{int(wait_seconds)}s'"))
        row = session.execute(
            text(
                "UPDATE locks SET locked_until = :new_until, holder = :holder "
                "WHERE name = :name AND (locked_until IS NULL OR locked_until < :now) "
                "RETURNING id"
            ),
            {"new_until": new_until, "holder": holder, "name": name, "now": now},
        ).first()
        session.execute(text("SET LOCAL lock_timeout = 0"))
    except Exception as exc:  # noqa: BLE001 -- only "someone else holds it" is swallowed
        if getattr(getattr(exc, "orig", None), "pgcode", None) != "55P03":  # lock_not_available
            raise
        session.rollback()
        return False
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


def release_lock(session, *, holder: str, name: str = "tick_lock") -> None:
    session.execute(
        text("UPDATE locks SET locked_until = NULL WHERE name = :name AND holder = :holder"),
        {"name": name, "holder": holder},
    )
