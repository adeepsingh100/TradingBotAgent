"""llm_calls: link a failed attempt to the fallback model that rescued it

`is_fallback` marks a row made by call_structured's fallback attempt;
`rescued_by` is set on the PRIMARY's failed row when that fallback then
answered. Lets Model Health report "% of calls answered" and stop
listing rescued first attempts as errors. Both nullable/defaulted, so
old worker code keeps inserting rows unchanged.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-05
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("llm_calls", sa.Column("is_fallback", sa.Boolean, nullable=False, server_default=sa.false()))
    op.add_column("llm_calls", sa.Column("rescued_by", sa.String))


def downgrade() -> None:
    op.drop_column("llm_calls", "rescued_by")
    op.drop_column("llm_calls", "is_fallback")
