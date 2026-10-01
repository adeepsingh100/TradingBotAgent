"""Engine/session setup for CockroachDB Serverless, plus a retry
wrapper for its SERIALIZABLE-by-default transaction conflicts
(SQLSTATE 40001) -- Postgres defaults to READ COMMITTED and rarely
needs this; CockroachDB doesn't, so any multi-statement write is a
candidate for a retryable conflict under real concurrency (two ticks
racing, however unlikely with the tick lock also in place).

No Postgres advisory locks anywhere in this app for the same reason
CockroachDB doesn't support them at all -- the tick lock (core/db/
models.py::Lock) is a plain conditional UPDATE, not pg_advisory_lock.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable, TypeVar

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from core.config import settings

_T = TypeVar("_T")

engine = create_engine(settings.database_url, pool_pre_ping=True)
SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def with_retry(fn: Callable[[Session], _T], *, max_attempts: int = 5, base_delay: float = 0.1) -> _T:
    """Runs `fn(session)` inside its own session/transaction, retrying
    on a CockroachDB serialization conflict (SQLSTATE 40001) with
    exponential backoff. Use for any write that isn't already covered
    by the tick lock (e.g. a dashboard control write racing a tick)."""
    last_exc: Exception | None = None
    for attempt in range(max_attempts):
        try:
            with get_session() as session:
                return fn(session)
        except Exception as exc:  # noqa: BLE001 -- re-raised if not retryable
            pgcode = getattr(getattr(exc, "orig", None), "pgcode", None)
            if pgcode != "40001" or attempt == max_attempts - 1:
                raise
            last_exc = exc
            time.sleep(base_delay * (2**attempt))
    raise last_exc  # pragma: no cover -- loop always returns or raises above
