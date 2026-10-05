"""SQLAlchemy 2.x models -- the only place table schema is defined
(Alembic migrations are generated FROM this file, never hand-edited to
add a column this file doesn't also have).

UUID primary keys, TIMESTAMPTZ everywhere, per spec section 11.

Status/lifecycle fields are plain `String`, validated at the Python
layer (Pydantic/StrEnum in the calling code), not a Postgres ENUM type
-- CockroachDB's ALTER TYPE ... ADD VALUE support and locking semantics
around enums are one more dialect gap worth avoiding for values that
will change during development (strategy lifecycle, alert types).

Design notes on two things the spec's table list doesn't spell out:

- `agents` is 1:1 with `wallets` (agents.wallet_id, unique, not null).
  Section 8's survival mechanics are wallet-scoped ("stop trading
  permanently for THAT wallet"), but section 11 lists `agents.status`/
  `died_at` separately from `wallets` -- the two tables are split
  exactly along that line: `wallets` is the capital/ledger, `agents` is
  the alive/paused/dead brain-state for that same capital. A second
  wallet gets a second agent row; strategies stay global (below).

- `strategies` is NOT wallet-scoped. It's "the strategy library" (spec
  section 5) -- the same EMA-crossover-with-these-params can be, and
  should be, evaluated across every wallet that tries it, so promotion
  evidence accumulates from all of them rather than resetting per
  wallet. This is also what makes "Reset agent" (section 8) clean:
  resetting a wallet clears that wallet's trades/positions/equity, and
  never touches the global strategy lifecycle/stats sitting on
  `strategies`.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default="now()", nullable=False)


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    firebase_uid: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    created_at: Mapped[datetime] = _created_at()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Wallet(Base):
    __tablename__ = "wallets"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(String, nullable=False, default="paper")  # paper | live
    starting_capital: Mapped[float] = mapped_column(Float, nullable=False)
    current_cash: Mapped[float] = mapped_column(Float, nullable=False)
    tds_credit: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = _created_at()

    agent: Mapped["Agent"] = relationship(back_populates="wallet", uselist=False)


class Agent(Base):
    __tablename__ = "agents"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id"), unique=True, nullable=False)
    status: Mapped[str] = mapped_column(String, nullable=False, default="alive")  # alive | paused | dead
    mode: Mapped[str] = mapped_column(String, nullable=False, default="paper")  # paper | live
    created_at: Mapped[datetime] = _created_at()
    died_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    wallet: Mapped[Wallet] = relationship(back_populates="agent")


class Strategy(Base):
    __tablename__ = "strategies"

    id: Mapped[uuid.UUID] = _uuid_pk()
    type: Mapped[str] = mapped_column(String, nullable=False)  # ema_crossover | rsi_mean_reversion | ...
    params: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False, default="draft")
    # draft -> backtested -> paper_testing -> approved_for_live -> live -> retired
    stats: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", onupdate=datetime.utcnow, nullable=False
    )


class Backtest(Base):
    __tablename__ = "backtests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    strategy_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("strategies.id"), nullable=False)
    params_snapshot: Mapped[dict] = mapped_column(JSON, nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    trade_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    net_pnl: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    win_rate: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    profit_factor: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    max_drawdown_pct: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    created_at: Mapped[datetime] = _created_at()


class Decision(Base):
    """One row per agent cycle, regardless of outcome -- a HOLD or a
    risk_check rejection still writes a row here, since the Agent Brain
    dashboard page shows the full decision timeline, not just executed
    trades."""

    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id"), nullable=False)
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("strategies.id"))
    proposal: Mapped[dict] = mapped_column(JSON, nullable=False)
    # {action, pair, size_inr, entry, stop_loss, take_profit, confidence, reasoning}
    risk_verdict: Mapped[str] = mapped_column(String, nullable=False)  # approved | downsized | rejected | hold
    risk_reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    # Not a declared ForeignKey -- trades.decision_id already points back
    # here, and a real FK in both directions is a circular dependency
    # between two brand-new tables that's needless risk to take on a
    # CockroachDB migration (see session.py's module docstring for the
    # kind of DDL friction this dialect has produced before). App code
    # only ever reads this after the trade row exists.
    executed_trade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    created_at: Mapped[datetime] = _created_at()


class Position(Base):
    """Every position ever opened, open or closed -- NOT deleted on
    close (`closed_at` marks that instead). First design was
    delete-on-close; a real DB caught the flaw immediately: trades.
    position_id's FK means the entry+exit Trade rows still reference
    this row forever, so deleting it is a foreign-key violation, not a
    cleanup. "Open positions" is `WHERE closed_at IS NULL`."""

    __tablename__ = "positions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id"), nullable=False)
    pair: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)  # buy | sell (buy == long, this is spot-only)
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss: Mapped[float] = mapped_column(Float, nullable=False)  # mandatory, risk_manager rejects without one
    take_profit: Mapped[float] = mapped_column(Float, nullable=False)
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("strategies.id"))
    decision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("decisions.id"))
    opened_at: Mapped[datetime] = _created_at()
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        # Partial index, not a plain UniqueConstraint -- a closed
        # position must not block opening a new one in the same pair
        # later. max_open_positions is a separate risk_manager count
        # check, not enforced here.
        Index("positions_wallet_pair_open_key", "wallet_id", "pair", unique=True, postgresql_where=text("closed_at IS NULL")),
    )


class Trade(Base):
    """One row per FILL (an entry buy and its later exit sell are two
    rows), matching real exchange trade-history semantics. `pnl` is
    null on an entry fill, populated on the fill that closes a
    position."""

    __tablename__ = "trades"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id"), nullable=False)
    position_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("positions.id"))
    pair: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)  # buy | sell
    qty: Mapped[float] = mapped_column(Float, nullable=False)
    price: Mapped[float] = mapped_column(Float, nullable=False)
    fee: Mapped[float] = mapped_column(Float, nullable=False)
    tds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    slippage: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    pnl: Mapped[float | None] = mapped_column(Float)
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("strategies.id"))
    decision_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("decisions.id"))
    executed_at: Mapped[datetime] = _created_at()


class AgentMemory(Base):
    __tablename__ = "agent_memory"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id"), nullable=False)
    strategy_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("strategies.id"))
    content: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = _created_at()


class EquityHistory(Base):
    """Both the agent's real equity curve AND the two benchmark curves
    live here, distinguished by `series` -- all three are plotted
    together per wallet (spec section 7), and keeping them in one table
    with the same shape is what makes that one query instead of three
    separately-shaped ones."""

    __tablename__ = "equity_history"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id"), nullable=False)
    series: Mapped[str] = mapped_column(String, nullable=False)  # agent | benchmark_btc | benchmark_cash
    equity_inr: Mapped[float] = mapped_column(Float, nullable=False)
    cash_inr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    holdings_value_inr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    tds_credit_inr: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    recorded_at: Mapped[datetime] = _created_at()

    __table_args__ = (UniqueConstraint("wallet_id", "series", "recorded_at", name="equity_history_series_point_key"),)


class LLMCall(Base):
    __tablename__ = "llm_calls"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wallet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wallets.id"))
    node: Mapped[str] = mapped_column(String, nullable=False)  # strategize | decide
    provider: Mapped[str] = mapped_column(String, nullable=False)
    model: Mapped[str] = mapped_column(String, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)
    # Migration 0004: a fallback attempt's own row has is_fallback=True; the
    # primary's failed row gets rescued_by=<fallback model> when it answered.
    is_fallback: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    rescued_by: Mapped[str | None] = mapped_column(String)
    created_at: Mapped[datetime] = _created_at()


class ControlCommand(Base):
    """The worker reads rows with status='pending' at the start of every
    tick, applies them, and flips status to 'applied' -- a simple
    command queue, not a live push channel (matches the stateless,
    poll-once-per-tick worker design in spec section 3)."""

    __tablename__ = "control_commands"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wallet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wallets.id"))  # null == applies to all wallets
    command: Mapped[str] = mapped_column(String, nullable=False)  # pause | resume | kill_switch | set_mode | ...
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending")  # pending | applied
    created_at: Mapped[datetime] = _created_at()
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Setting(Base):
    """Key-value store for every behavioral knob the dashboard's
    Controls & Settings page edits live (risk params, fee/TDS/slippage
    rates, watchlist, LLM provider/model, Telegram toggles) -- see
    core/config.py's module docstring for the split rationale."""

    __tablename__ = "settings"

    id: Mapped[uuid.UUID] = _uuid_pk()
    key: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    value: Mapped[dict] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", onupdate=datetime.utcnow, nullable=False
    )


class MarketCache(Base):
    """Refreshed daily from CoinDCX's markets_details -- see
    core/coindcx/market_cache.py. Every order must round to these
    before submission or CoinDCX rejects it."""

    __tablename__ = "market_cache"

    id: Mapped[uuid.UUID] = _uuid_pk()
    pair: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    min_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    max_quantity: Mapped[float] = mapped_column(Float, nullable=False)
    step: Mapped[float] = mapped_column(Float, nullable=False)
    min_notional: Mapped[float] = mapped_column(Float, nullable=False)
    base_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    target_precision: Mapped[int] = mapped_column(Integer, nullable=False)
    order_types: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    raw: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    refreshed_at: Mapped[datetime] = _created_at()


class Lock(Base):
    """One row per named lock (v1 only ever needs 'tick_lock') --
    acquire via a conditional UPDATE/INSERT where locked_until < now(),
    never a Postgres advisory lock (CockroachDB doesn't support those;
    see core/db/session.py)."""

    __tablename__ = "locks"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    holder: Mapped[str] = mapped_column(String, nullable=False, default="")


class Heartbeat(Base):
    """One row per named source (v1 only needs 'worker_tick'), upserted
    every tick -- the dashboard's heartbeat warning compares
    last_beat_at against now()."""

    __tablename__ = "heartbeats"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source: Mapped[str] = mapped_column(String, unique=True, nullable=False)
    last_beat_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default="now()", nullable=False)
    detail: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class Log(Base):
    __tablename__ = "logs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    level: Mapped[str] = mapped_column(String, nullable=False)  # info | warning | error
    source: Mapped[str] = mapped_column(String, nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = _created_at()


class LiveOrder(Base):
    """Audit trail for every REAL order `core/live_engine.py` ever
    attempts -- written BEFORE the exchange call (status
    'pending_submit'), updated as the real order resolves. This is the
    ground truth that makes a crash mid-placement reconstructable from
    the DB + the exchange's own order history, which is why
    live_engine commits each state transition immediately rather than
    waiting for the tick's outer transaction (see live_engine.py's
    module docstring)."""

    __tablename__ = "live_orders"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wallet_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("wallets.id"), nullable=False)
    # Plain UUID columns, not FKs -- same reasoning as Decision.executed_trade_id:
    # this row is written before the position/trade/decision it ends up
    # pointing at necessarily exist yet.
    decision_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    position_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    trade_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    pair: Mapped[str] = mapped_column(String, nullable=False)
    side: Mapped[str] = mapped_column(String, nullable=False)  # buy | sell
    client_order_id: Mapped[str] = mapped_column(String, nullable=False)
    exchange_order_id: Mapped[str | None] = mapped_column(String)
    status: Mapped[str] = mapped_column(String, nullable=False, default="pending_submit")
    # pending_submit | submitted | filled | partially_filled |
    # timed_out_cancelled | error | unknown_needs_manual_check
    requested_qty: Mapped[float] = mapped_column(Float, nullable=False)
    filled_qty: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    filled_price: Mapped[float | None] = mapped_column(Float)
    raw_response: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default="now()", onupdate=datetime.utcnow, nullable=False
    )


class AlertSent(Base):
    """Logs every Telegram alert actually sent -- lets the worker avoid
    re-sending a stateful alert (e.g. heartbeat-missing) every single
    tick once it's already gone out once."""

    __tablename__ = "alerts_sent"

    id: Mapped[uuid.UUID] = _uuid_pk()
    wallet_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("wallets.id"))  # null == system-wide alert
    alert_type: Mapped[str] = mapped_column(String, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    sent_at: Mapped[datetime] = _created_at()
