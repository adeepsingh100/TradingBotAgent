"""The one query shared by every page -- the wallet picker. Everything
else is specific enough to its own page that inlining it there reads
better than a speculative shared helper."""

from __future__ import annotations

from core.db.models import Wallet


def list_wallets(session) -> list[Wallet]:
    return session.query(Wallet).order_by(Wallet.name).all()
