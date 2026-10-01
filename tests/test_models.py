"""Schema sanity check -- no real DB (repo convention: the suite never
makes a real network/DB call). Confirms every table in core/db/models.py
compiles to valid Postgres-dialect DDL (what CockroachDB actually
speaks) and that FK resolution order has no unresolved/circular
reference -- `sorted_tables` raises on a real cycle, which is exactly
the bug the executed_trade_id design note in models.py avoids."""

from __future__ import annotations

from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from core.db.models import Base

EXPECTED_TABLES = {
    "users", "wallets", "agents", "strategies", "backtests", "decisions",
    "positions", "trades", "agent_memory", "equity_history", "llm_calls",
    "control_commands", "settings", "market_cache", "locks", "heartbeats",
    "logs", "alerts_sent", "live_orders",
}


def test_every_expected_table_is_registered():
    assert set(Base.metadata.tables.keys()) == EXPECTED_TABLES


def test_all_tables_compile_to_valid_postgres_ddl_in_fk_order():
    dialect = postgresql.dialect()
    for table in Base.metadata.sorted_tables:  # raises CircularDependencyError on an unresolved FK cycle
        ddl = str(CreateTable(table).compile(dialect=dialect))
        assert table.name in ddl
