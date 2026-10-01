"""No real DB/network -- core.coindcx.client.get_candles and
worker.agent.nodes.call_structured are monkeypatched; FakeSession
(tests/conftest.py) stands in for SQLAlchemy's Session."""

from __future__ import annotations

import uuid

import pytest

from core.coindcx import client
from core.db.models import Decision, MarketCache, Position, Strategy, Trade, Wallet
from core.risk_manager import Proposal
from core.strategies.base import StrategySignal
from tests.conftest import FakeSession, make_candles
from worker.agent import nodes
from worker.agent.schemas import StrategizeOutput, StrategyAssignment

COSTS = {"maker_fee_pct": 0.2, "taker_fee_pct": 0.2, "tds_pct": 1.0, "slippage_pct": 0.1}
RISK = {
    "max_position_size_pct": 20, "max_risk_per_trade_pct": 3, "daily_loss_limit_pct": 8,
    "max_open_positions": 2, "max_trades_per_day": 4, "min_reward_to_cost_multiple": 1.5,
    "min_confidence": 0.6, "cooldown_hours_after_losses": 4, "consecutive_losses_trigger": 3,
}

# Proven one-candle up-cross fixture (same shape as tests/test_backtester.py's
# stop-loss test) -- EMA5/EMA10 cross exactly on the last candle.
_BUY_CANDLES = make_candles([100.0] * 15 + [105.0])
_EMA_PARAMS = {"fast_period": 5, "slow_period": 10, "stop_loss_pct": 2.0, "take_profit_pct": 20.0}


def _wallet():
    return Wallet(id=uuid.uuid4(), name="test", kind="paper", starting_capital=1000.0, current_cash=1000.0, tds_credit=0.0)


def _live_wallet():
    return Wallet(id=uuid.uuid4(), name="live", kind="live", starting_capital=1000.0, current_cash=1000.0, tds_credit=0.0)


def _strategy():
    return Strategy(id=uuid.uuid4(), type="ema_crossover", params=_EMA_PARAMS, status="approved_for_live", stats={})


def _market_cache_row():
    return MarketCache(pair="I-BTC_INR", min_quantity=0.0001, max_quantity=10, step=0.0001,
                        min_notional=1, base_precision=2, target_precision=4, raw={"symbol": "BTCINR"})


# --- load_market ---


def test_load_market_fetches_per_pair_and_reverses_to_oldest_first(monkeypatch):
    def fake_get_candles(pair_code, interval, limit=500):
        return [{"close": 300, "high": 300, "low": 300, "time": 2}, {"close": 100, "high": 100, "low": 100, "time": 0}]

    monkeypatch.setattr(client, "get_candles", fake_get_candles)
    state = {"pair_map": {"BTCINR": "I-BTC_INR"}}

    result = nodes.load_market(state)

    assert result["candles"]["BTCINR"][0]["close"] == 100  # oldest first
    assert result["prices"]["BTCINR"] == 300  # last candle's close


# --- check_exits ---


def test_check_exits_closes_a_position_whose_stop_was_hit():
    session = FakeSession()
    wallet = _wallet()
    position = Position(id=uuid.uuid4(), wallet_id=wallet.id, pair="BTCINR", side="buy", qty=0.001,
                         entry_price=100.0, stop_loss=90.0, take_profit=120.0)
    session.add(Trade(wallet_id=wallet.id, position_id=position.id, pair="BTCINR", side="buy",
                       qty=0.001, price=100.0, fee=0.0))  # close_position needs the entry fill on record
    state = {
        "session": session, "wallet": wallet, "costs_settings": COSTS,
        "open_positions": [position], "candles": {"BTCINR": [{"high": 95, "low": 85, "close": 88}]},
        "prices": {"BTCINR": 88},
    }

    result = nodes.check_exits(state)

    assert result["open_positions"] == []
    assert position.closed_at is not None


def test_check_exits_leaves_untouched_when_nothing_triggers():
    session = FakeSession()
    wallet = _wallet()
    position = Position(id=uuid.uuid4(), wallet_id=wallet.id, pair="BTCINR", side="buy", qty=0.001,
                         entry_price=100.0, stop_loss=90.0, take_profit=120.0)
    state = {
        "session": session, "wallet": wallet, "costs_settings": COSTS,
        "open_positions": [position], "candles": {"BTCINR": [{"high": 105, "low": 95, "close": 100}]},
        "prices": {"BTCINR": 100},
    }

    result = nodes.check_exits(state)

    assert result["open_positions"] == [position]
    assert position.closed_at is None


def test_check_exits_closes_a_live_position_via_live_engine(monkeypatch):
    session = FakeSession()
    wallet = _live_wallet()
    position = Position(id=uuid.uuid4(), wallet_id=wallet.id, pair="BTCINR", side="buy", qty=0.001,
                         entry_price=100.0, stop_loss=90.0, take_profit=120.0)

    def fake_close_position(session, wallet, position, **kw):
        position.closed_at = "set"  # sentinel -- nodes.py only checks it's not None
        return Trade(wallet_id=wallet.id, position_id=position.id, pair="BTCINR", side="sell", qty=0.001,
                     price=88.0, fee=0.0, pnl=-0.012)

    monkeypatch.setattr(nodes.live_engine, "close_position", fake_close_position)
    state = {
        "session": session, "wallet": wallet, "costs_settings": COSTS,
        "open_positions": [position], "candles": {"BTCINR": [{"high": 95, "low": 85, "close": 88}]},
        "prices": {"BTCINR": 88}, "market_cache": {"BTCINR": _market_cache_row()}, "live_trading_enabled": True,
    }

    result = nodes.check_exits(state)

    assert result["open_positions"] == []
    assert position.closed_at == "set"


def test_check_exits_leaves_a_live_position_open_when_live_engine_cant_resolve_it(monkeypatch):
    session = FakeSession()
    wallet = _live_wallet()
    position = Position(id=uuid.uuid4(), wallet_id=wallet.id, pair="BTCINR", side="buy", qty=0.001,
                         entry_price=100.0, stop_loss=90.0, take_profit=120.0)

    monkeypatch.setattr(nodes.live_engine, "close_position", lambda *a, **kw: None)  # unresolved this tick
    state = {
        "session": session, "wallet": wallet, "costs_settings": COSTS,
        "open_positions": [position], "candles": {"BTCINR": [{"high": 95, "low": 85, "close": 88}]},
        "prices": {"BTCINR": 88}, "market_cache": {"BTCINR": _market_cache_row()}, "live_trading_enabled": True,
    }

    result = nodes.check_exits(state)

    assert result["open_positions"] == [position]  # stays open -- retried next tick


# --- strategize ---


def test_strategize_returns_empty_assignment_without_an_llm():
    state = {"open_positions": [], "watchlist": ["BTCINR"], "llm": None, "candles": {}, "strategies": {}}
    assert nodes.strategize(state) == {"strategy_assignment": {}}


def test_strategize_skips_pairs_already_open():
    position = Position(pair="BTCINR", side="buy", qty=1, entry_price=1, stop_loss=1, take_profit=1)
    state = {"open_positions": [position], "watchlist": ["BTCINR"], "llm": object(), "candles": {}, "strategies": {}}
    assert nodes.strategize(state) == {"strategy_assignment": {}}


def test_strategize_builds_assignment_from_llm_output(monkeypatch):
    session = FakeSession()
    wallet = _wallet()
    sent = {}

    def fake_call(session, llm, schema, messages, **kw):
        sent["prompt"] = messages[0]["content"]
        return StrategizeOutput(assignments=[StrategyAssignment(pair="BTCINR", strategy_type="ema_crossover")])

    monkeypatch.setattr(nodes, "call_structured", fake_call)
    state = {
        "session": session, "wallet": wallet, "open_positions": [], "watchlist": ["BTCINR"], "llm": object(),
        "candles": {"BTCINR": make_candles([100.0 + i for i in range(25)])},
        "strategies": {"ema_crossover": _strategy()}, "llm_provider": "nvidia", "llm_model": "m",
        "equity": 800.0, "death_threshold_pct": 50.0,
    }

    result = nodes.strategize(state)

    assert result["strategy_assignment"] == {"BTCINR": "ema_crossover"}
    # Survival brief: money left and distance to death (1000 * 50% = 500 death line).
    assert "You now have 800.00 INR (-20.00%)" in sent["prompt"]
    assert "below 500.00 INR you die" in sent["prompt"]
    assert "300.00 INR away from death" in sent["prompt"]


# --- generate_mechanical_signals ---


def test_generate_mechanical_signals_produces_a_buy_signal():
    strategy = _strategy()
    state = {"strategy_assignment": {"BTCINR": "ema_crossover"}, "strategies": {"ema_crossover": strategy},
             "candles": {"BTCINR": _BUY_CANDLES}}

    result = nodes.generate_mechanical_signals(state)

    strat, signal = result["signals"]["BTCINR"]
    assert strat is strategy
    assert signal.action == "buy"


# --- decide ---


def test_decide_never_calls_llm_for_a_non_buy_signal(monkeypatch):
    called = []
    monkeypatch.setattr(nodes, "call_structured", lambda *a, **kw: called.append(1))
    strategy = _strategy()
    hold_signal = StrategySignal(action="hold", reasoning="no crossover")
    state = {"signals": {"BTCINR": (strategy, hold_signal)}, "llm": object(), "prices": {}, "equity": 1000.0,
             "risk_settings": RISK, "session": FakeSession(), "wallet": _wallet(),
             "llm_provider": "nvidia", "llm_model": "m"}

    result = nodes.decide(state)

    assert result["proposals"] == {}
    assert called == []


def test_decide_calls_llm_for_a_buy_signal_and_stores_the_proposal(monkeypatch):
    fake_proposal = Proposal(action="buy", pair="BTCINR", confidence=0.8, size_inr=100, reasoning="looks good")
    monkeypatch.setattr(nodes, "call_structured", lambda *a, **kw: fake_proposal)
    strategy = _strategy()
    buy_signal = StrategySignal(action="buy", stop_loss=90.0, take_profit=120.0, reasoning="crossed up")
    state = {"signals": {"BTCINR": (strategy, buy_signal)}, "llm": object(), "prices": {"BTCINR": 100.0},
             "equity": 1000.0, "risk_settings": RISK, "session": FakeSession(), "wallet": _wallet(),
             "llm_provider": "nvidia", "llm_model": "m"}

    result = nodes.decide(state)

    assert result["proposals"]["BTCINR"] is fake_proposal


def test_decide_records_none_when_llm_unavailable():
    strategy = _strategy()
    buy_signal = StrategySignal(action="buy", stop_loss=90.0, take_profit=120.0, reasoning="crossed up")
    state = {"signals": {"BTCINR": (strategy, buy_signal)}, "llm": None, "prices": {"BTCINR": 100.0}}

    result = nodes.decide(state)

    assert result["proposals"]["BTCINR"] is None


# --- risk_and_execute ---


def test_risk_and_execute_logs_a_hold_decision_for_a_non_buy_signal():
    session = FakeSession()
    wallet = _wallet()
    strategy = _strategy()
    hold_signal = StrategySignal(action="hold", reasoning="no crossover")
    state = {
        "session": session, "wallet": wallet, "signals": {"BTCINR": (strategy, hold_signal)}, "proposals": {},
        "prices": {}, "equity": 1000.0, "open_positions": [], "trades_today_count": 0, "daily_pnl_pct": 0.0,
        "cooldown_until": None, "kill_switch": False, "risk_settings": RISK, "costs_settings": COSTS,
    }

    result = nodes.risk_and_execute(state)

    assert result["results"] == [{"pair": "BTCINR", "verdict": "hold", "reason": "no buy signal"}]
    decisions = [d for d in session.added if isinstance(d, Decision)]
    assert len(decisions) == 1 and decisions[0].risk_verdict == "hold"


def test_risk_and_execute_opens_a_position_on_an_approved_proposal():
    session = FakeSession()
    wallet = _wallet()
    strategy = _strategy()
    buy_signal = StrategySignal(action="buy", stop_loss=90.0, take_profit=120.0, reasoning="crossed up")
    proposal = Proposal(action="buy", pair="BTCINR", confidence=0.9, size_inr=100.0)
    state = {
        "session": session, "wallet": wallet, "signals": {"BTCINR": (strategy, buy_signal)},
        "proposals": {"BTCINR": proposal}, "prices": {"BTCINR": 100.0}, "equity": 1000.0,
        "open_positions": [], "trades_today_count": 0, "daily_pnl_pct": 0.0, "cooldown_until": None,
        "kill_switch": False, "risk_settings": RISK, "costs_settings": COSTS,
    }

    result = nodes.risk_and_execute(state)

    assert result["results"][0]["verdict"] in ("approved", "downsized")
    positions = [p for p in session.added if isinstance(p, Position)]
    assert len(positions) == 1 and positions[0].pair == "BTCINR"
    decisions = [d for d in session.added if isinstance(d, Decision)]
    assert decisions[0].executed_trade_id == positions[0].id


def test_risk_and_execute_opens_a_live_position_via_live_engine_on_approval(monkeypatch):
    session = FakeSession()
    wallet = _live_wallet()
    strategy = _strategy()
    buy_signal = StrategySignal(action="buy", stop_loss=90.0, take_profit=120.0, reasoning="crossed up")
    proposal = Proposal(action="buy", pair="BTCINR", confidence=0.9, size_inr=100.0)

    fake_position = Position(id=uuid.uuid4(), wallet_id=wallet.id, pair="BTCINR", side="buy", qty=1,
                              entry_price=100.0, stop_loss=90.0, take_profit=120.0)
    captured = {}

    def fake_open_position(session, wallet, **kw):
        captured.update(kw)
        return fake_position

    monkeypatch.setattr(nodes.live_engine, "open_position", fake_open_position)
    state = {
        "session": session, "wallet": wallet, "signals": {"BTCINR": (strategy, buy_signal)},
        "proposals": {"BTCINR": proposal}, "prices": {"BTCINR": 100.0}, "equity": 1000.0,
        "open_positions": [], "trades_today_count": 0, "daily_pnl_pct": 0.0, "cooldown_until": None,
        "kill_switch": False, "risk_settings": RISK, "costs_settings": COSTS,
        "market_cache": {"BTCINR": _market_cache_row()}, "live_trading_enabled": True,
    }

    result = nodes.risk_and_execute(state)

    assert result["results"][0]["verdict"] in ("approved", "downsized")
    assert captured["allow_live"] is True
    decisions = [d for d in session.added if isinstance(d, Decision)]
    assert decisions[0].executed_trade_id == fake_position.id
    assert [p for p in session.added if isinstance(p, Position)] == []  # live_engine, not paper_engine, owns the write


def test_risk_and_execute_skips_live_entry_without_a_market_cache_row():
    session = FakeSession()
    wallet = _live_wallet()
    strategy = _strategy()
    buy_signal = StrategySignal(action="buy", stop_loss=90.0, take_profit=120.0, reasoning="crossed up")
    proposal = Proposal(action="buy", pair="BTCINR", confidence=0.9, size_inr=100.0)
    state = {
        "session": session, "wallet": wallet, "signals": {"BTCINR": (strategy, buy_signal)},
        "proposals": {"BTCINR": proposal}, "prices": {"BTCINR": 100.0}, "equity": 1000.0,
        "open_positions": [], "trades_today_count": 0, "daily_pnl_pct": 0.0, "cooldown_until": None,
        "kill_switch": False, "risk_settings": RISK, "costs_settings": COSTS,
        "market_cache": {}, "live_trading_enabled": True,
    }

    result = nodes.risk_and_execute(state)

    assert result["results"][0]["verdict"] in ("approved", "downsized")
    decisions = [d for d in session.added if isinstance(d, Decision)]
    assert decisions[0].executed_trade_id is None


def test_risk_and_execute_falls_back_to_hold_when_llm_proposal_is_none():
    session = FakeSession()
    wallet = _wallet()
    strategy = _strategy()
    buy_signal = StrategySignal(action="buy", stop_loss=90.0, take_profit=120.0, reasoning="crossed up")
    state = {
        "session": session, "wallet": wallet, "signals": {"BTCINR": (strategy, buy_signal)},
        "proposals": {"BTCINR": None}, "prices": {"BTCINR": 100.0}, "equity": 1000.0,
        "open_positions": [], "trades_today_count": 0, "daily_pnl_pct": 0.0, "cooldown_until": None,
        "kill_switch": False, "risk_settings": RISK, "costs_settings": COSTS,
    }

    result = nodes.risk_and_execute(state)

    assert result["results"][0]["verdict"] == "hold"
    assert [p for p in session.added if isinstance(p, Position)] == []
