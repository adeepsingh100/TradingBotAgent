"""positions: add closed_at, replace the plain unique constraint with a
partial unique index that only applies while a position is still open

Found this was wrong, not just incomplete, while live-testing Phase 3:
0001's design deleted a Position row on close, but trades.position_id's
FK means the entry+exit Trade rows still reference it -- the DELETE is
a foreign-key violation against a real DB, caught immediately
(CockroachDB and real Postgres both enforce this the same way; this
isn't a CockroachDB-specific gap).

Revision ID: 0002
Revises: 0001
Create Date: 2026-10-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0002"
down_revision: Union[str, None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("positions", sa.Column("closed_at", sa.DateTime(timezone=True)))
    op.drop_constraint("positions_wallet_pair_key", "positions", type_="unique")
    op.create_index(
        "positions_wallet_pair_open_key", "positions", ["wallet_id", "pair"],
        unique=True, postgresql_where=sa.text("closed_at IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("positions_wallet_pair_open_key", table_name="positions")
    op.create_unique_constraint("positions_wallet_pair_key", "positions", ["wallet_id", "pair"])
    op.drop_column("positions", "closed_at")
