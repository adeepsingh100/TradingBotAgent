"""live_orders: audit trail for real CoinDCX order placement (Phase 7)

Written before the exchange call and updated as a real order resolves
-- see core/db/models.py::LiveOrder's docstring and core/live_engine.py
for why this table exists and why it's committed independently of the
tick's outer transaction.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

UUID = postgresql.UUID(as_uuid=True)
TS = sa.DateTime(timezone=True)


def upgrade() -> None:
    op.create_table(
        "live_orders",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("wallet_id", UUID, sa.ForeignKey("wallets.id"), nullable=False),
        sa.Column("decision_id", UUID),
        sa.Column("position_id", UUID),
        sa.Column("trade_id", UUID),
        sa.Column("pair", sa.String, nullable=False),
        sa.Column("side", sa.String, nullable=False),
        sa.Column("client_order_id", sa.String, nullable=False),
        sa.Column("exchange_order_id", sa.String),
        sa.Column("status", sa.String, nullable=False),
        sa.Column("requested_qty", sa.Float, nullable=False),
        sa.Column("filled_qty", sa.Float, nullable=False),
        sa.Column("filled_price", sa.Float),
        sa.Column("raw_response", sa.JSON, nullable=False),
        sa.Column("error", sa.Text),
        sa.Column("created_at", TS, nullable=False, server_default=sa.text("now()")),
        sa.Column("updated_at", TS, nullable=False, server_default=sa.text("now()")),
    )


def downgrade() -> None:
    op.drop_table("live_orders")
