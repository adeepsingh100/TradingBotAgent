"""Integration test for the compiled LangGraph wiring itself (not just
the node functions in isolation, which tests/test_agent_nodes.py
already covers) -- proves state actually flows load_market ->
check_exits -> strategize -> generate_mechanical_signals -> decide ->
risk_and_execute end to end. No real network/DB/LLM: core.coindcx.
client.get_candles and worker.agent.nodes.call_structured are
monkeypatched."""

from __future__ import annotations

import uuid

from core.coindcx import client
from core.db.models import Decision, Position, Strategy, Wallet
from core.risk_manager import Proposal
from tests.conftest import FakeSession, make_candles
from worker.agent import nodes
from worker.agent.graph import build_graph
from worker.agent.schemas import StrategizeOutput, StrategyAssignment

COSTS = {"maker_fee_pct": 0.2, "taker_fee_pct": 0.2, "tds_pct": 1.0, "slippage_pct": 0.1}
RISK = {
    "max_position_size_pct": 20, "max_risk_per_trade_pct": 3, "daily_loss_limit_pct": 8,
    "max_open_positions": 2, "max_trades_per_day": 4, "min_reward_to_cost_multiple": 1.5,
    "min_confidence": 0.6, "cooldown_hours_after_losses": 4, "consecutive_losses_trigger": 3,
}
_EMA_PARAMS = {"fast_period": 5, "slow_period": 10, "stop_loss_pct": 2.0, "take_profit_pct": 20.0}


def test_graph_runs_end_to_end_with_no_llm_and_does_nothing():
    session = FakeSession()
    wallet = Wallet(id=uuid.uuid4(), name="test", kind="paper", starting_capital=1000.0, current_cash=1000.0)
    state = {
        "session": session, "wallet": wallet, "watchlist": ["BTCINR"], "pair_map": {"BTCINR": "I-BTC_INR"},
        "open_positions": [], "strategies": {}, "risk_settings": RISK, "costs_settings": COSTS,
        "equity": 0.0, "trades_today_count": 0, "daily_pnl_pct": 0.0, "cooldown_until": None,
        "kill_switch": False, "llm": None, "llm_provider": "nvidia", "llm_model": "m",
        "candles": {}, "prices": {}, "strategy_assignment": {}, "signals": {}, "proposals": {}, "results": [],
    }

    graph = build_graph()
    result = graph.invoke(state)

    assert result["results"] == []


def test_graph_runs_end_to_end_and_opens_a_position_on_a_full_buy_path(monkeypatch):
    # Candles that make ema_crossover(5, 10) cross up on the LAST candle --
    # proven shape from tests/test_backtester.py, with extra flat lead-in
    # candles so it also clears strategize()'s own 20-candle context
    # requirement (a longer flat run ahead of the jump doesn't change
    # where the EMAs cross).
    rows = list(reversed(make_candles([100.0] * 19 + [105.0])))  # client returns newest-first
    monkeypatch.setattr(client, "get_candles", lambda pair_code, interval, limit=500: rows)
    monkeypatch.setattr(
        nodes, "call_structured",
        lambda session, llm, schema, messages, **kw: (
            StrategizeOutput(assignments=[StrategyAssignment(pair="BTCINR", strategy_type="ema_crossover")])
            if schema is StrategizeOutput
            else Proposal(action="buy", pair="BTCINR", confidence=0.9, size_inr=100.0)
        ),
    )

    session = FakeSession()
    wallet = Wallet(id=uuid.uuid4(), name="test", kind="paper", starting_capital=1000.0, current_cash=1000.0)
    strategy = Strategy(id=uuid.uuid4(), type="ema_crossover", params=_EMA_PARAMS, status="backtested", stats={})
    state = {
        "session": session, "wallet": wallet, "watchlist": ["BTCINR"], "pair_map": {"BTCINR": "I-BTC_INR"},
        "open_positions": [], "strategies": {"ema_crossover": strategy}, "risk_settings": RISK, "costs_settings": COSTS,
        "equity": 0.0, "trades_today_count": 0, "daily_pnl_pct": 0.0, "cooldown_until": None,
        "kill_switch": False, "llm": object(), "llm_provider": "nvidia", "llm_model": "m",
        "candles": {}, "prices": {}, "strategy_assignment": {}, "signals": {}, "proposals": {}, "results": [],
    }

    graph = build_graph()
    result = graph.invoke(state)

    assert result["results"][0]["verdict"] in ("approved", "downsized")
    assert any(isinstance(o, Position) for o in session.added)
    assert any(isinstance(o, Decision) for o in session.added)
