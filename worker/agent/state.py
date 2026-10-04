"""One `AgentState` per wallet per tick -- the graph is invoked once
per alive wallet (worker/cycle.py), not once for the whole system.
Holds live ORM objects (the open `session`, `wallet` row, etc.)
directly rather than serializing them, since the graph is compiled
with NO checkpointer (see graph.py's docstring) -- state never leaves
this one in-process `.invoke()` call, so there's nothing to serialize.
"""

from __future__ import annotations

from typing import Any, TypedDict


class AgentState(TypedDict, total=False):
    session: Any
    wallet: Any
    agent: Any
    watchlist: list[str]
    pair_map: dict[str, str]  # symbol ("BTCINR") -> CoinDCX pair code ("I-BTC_INR")
    market_cache: dict[str, Any]  # symbol -> MarketCache row (live order sizing/validation)
    live_trading_enabled: bool  # settings.live_trading.enabled, only meaningful for wallet.kind == "live"
    candles: dict[str, list[dict]]
    prices: dict[str, float]
    open_positions: list[Any]
    strategies: dict[str, Any]  # strategy_type -> Strategy row
    risk_settings: dict
    costs_settings: dict
    equity: float
    trades_today_count: int
    daily_pnl_pct: float
    cooldown_until: Any
    kill_switch: bool
    death_threshold_pct: float  # equity below starting_capital * this % kills the wallet
    rent_paid_inr: float  # running cost paid so far (core/running_cost.py); `equity` is already net of it
    daily_cost_inr: float  # this wallet's running cost per day
    llm: Any
    llm_provider: str
    llm_model: str
    llm_fallback: Any  # (llm, provider, model) tried once if the primary fails, or None
    strategy_assignment: dict[str, str]
    signals: dict[str, tuple[Any, Any]]  # symbol -> (Strategy row, StrategySignal)
    proposals: dict[str, Any]
    results: list[dict]
