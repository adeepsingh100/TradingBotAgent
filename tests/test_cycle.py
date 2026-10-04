"""No real DB/network -- FakeSession (tests/conftest.py) plus targeted
monkeypatches of cycle.py's own collaborators. _run_wallet_tick's
actual graph execution is already covered by test_agent_graph.py /
test_agent_nodes.py; here it's monkeypatched out so these tests stay
about run_cycle's own orchestration (lock, per-agent loop, death
check, heartbeat), not a re-test of the graph."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from core.db.models import (
    Agent, ControlCommand, Decision, EquityHistory, Heartbeat, LiveOrder, MarketCache, Position, Setting, Strategy,
    Trade, Wallet,
)
from tests.conftest import FakeSession
from worker import cycle

RISK = {"consecutive_losses_trigger": 2, "cooldown_hours_after_losses": 4}


@pytest.fixture(autouse=True)
def _no_live_ticker(monkeypatch):
    """run_cycle picks paper pairs from the live ticker -- make it fail so
    these tests stay offline and fall back to the `watchlist` setting."""
    def boom():
        raise RuntimeError("no network in tests")
    monkeypatch.setattr(cycle.coindcx_client, "get_ticker", boom)
    monkeypatch.setattr(cycle, "release_lock", lambda session, holder: None)
    monkeypatch.setattr(cycle, "research_due", lambda session, every_hours: False)


# --- _auto_watchlist ---


def test_auto_watchlist_uses_ticker_ranking(monkeypatch):
    monkeypatch.setattr(cycle.coindcx_client, "get_ticker", lambda: [
        {"market": "ETHINR", "volume": "9000000", "last_price": "100", "bid": "99.9", "ask": "100", "change_24_hour": "1"},
    ])
    assert cycle._auto_watchlist({}, ["BTCINR"]) == ["ETHINR"]


def test_auto_watchlist_falls_back_when_ticker_fails():
    assert cycle._auto_watchlist({}, ["BTCINR"]) == ["BTCINR"]


def _wallet(**kw):
    defaults = dict(id=uuid.uuid4(), name="small", kind="paper", starting_capital=1000.0, current_cash=1000.0, tds_credit=0.0)
    defaults.update(kw)
    return Wallet(**defaults)


# --- _pair_map ---


def test_pair_map_filters_to_watchlist_symbols_only():
    session = FakeSession()
    session.add(MarketCache(pair="I-BTC_INR", min_quantity=0, max_quantity=1, step=0.0001, min_notional=1,
                             base_precision=2, target_precision=4, raw={"symbol": "BTCINR"}))
    session.add(MarketCache(pair="I-ETH_INR", min_quantity=0, max_quantity=1, step=0.0001, min_notional=1,
                             base_precision=2, target_precision=4, raw={"symbol": "ETHINR"}))

    result = cycle._pair_map(session, ["BTCINR"])

    assert result == {"BTCINR": "I-BTC_INR"}


# --- _wallet_risk_state ---


def test_wallet_risk_state_counts_only_todays_buys():
    session = FakeSession()
    wallet = _wallet()
    today = datetime.now(timezone.utc)
    yesterday = today - timedelta(days=1)
    session.add(Trade(wallet_id=wallet.id, pair="BTCINR", side="buy", qty=1, price=1, fee=0, executed_at=today))
    session.add(Trade(wallet_id=wallet.id, pair="BTCINR", side="buy", qty=1, price=1, fee=0, executed_at=yesterday))

    result = cycle._wallet_risk_state(session, wallet, RISK)

    assert result["trades_today_count"] == 1


def test_wallet_risk_state_triggers_cooldown_after_consecutive_losses():
    session = FakeSession()
    wallet = _wallet()
    now = datetime.now(timezone.utc)
    session.add(Trade(wallet_id=wallet.id, pair="BTCINR", side="sell", qty=1, price=1, fee=0, pnl=-10, executed_at=now))
    session.add(Trade(wallet_id=wallet.id, pair="BTCINR", side="sell", qty=1, price=1, fee=0, pnl=-5,
                       executed_at=now - timedelta(hours=1)))

    result = cycle._wallet_risk_state(session, wallet, RISK)

    assert result["cooldown_until"] == now + timedelta(hours=4)


def test_wallet_risk_state_no_cooldown_after_a_recent_win():
    session = FakeSession()
    wallet = _wallet()
    now = datetime.now(timezone.utc)
    session.add(Trade(wallet_id=wallet.id, pair="BTCINR", side="sell", qty=1, price=1, fee=0, pnl=10, executed_at=now))
    session.add(Trade(wallet_id=wallet.id, pair="BTCINR", side="sell", qty=1, price=1, fee=0, pnl=-5,
                       executed_at=now - timedelta(hours=1)))

    result = cycle._wallet_risk_state(session, wallet, RISK)

    assert result["cooldown_until"] is None


# --- _apply_pending_resets ---


def test_apply_pending_resets_wipes_the_wallet_and_marks_the_command_applied():
    session = FakeSession()
    wallet = _wallet(name="small", current_cash=1.0, starting_capital=1000.0)
    agent = Agent(id=uuid.uuid4(), wallet_id=wallet.id, status="dead", mode="paper")
    agent.wallet = wallet
    session.add(agent)
    position = Position(id=uuid.uuid4(), wallet_id=wallet.id, pair="BTCINR", side="buy", qty=1,
                         entry_price=1, stop_loss=1, take_profit=1)
    session.add(position)
    session.add(Trade(wallet_id=wallet.id, position_id=position.id, pair="BTCINR", side="buy", qty=1, price=1, fee=0))
    session.add(Decision(wallet_id=wallet.id, proposal={}, risk_verdict="hold", risk_reason=""))
    session.add(EquityHistory(wallet_id=wallet.id, series="agent", equity_inr=1.0))
    command = ControlCommand(id=uuid.uuid4(), wallet_id=wallet.id, command="reset_wallet", payload={}, status="pending")
    session.add(command)

    cycle._apply_pending_resets(session)

    assert session.query(Trade).all() == []
    assert session.query(Position).all() == []
    assert session.query(Decision).all() == []
    assert session.query(EquityHistory).all() == []
    assert wallet.current_cash == 1000.0
    assert agent.status == "alive"
    assert agent.died_at is None
    assert command.status == "applied"


def test_apply_pending_resets_ignores_already_applied_commands():
    session = FakeSession()
    wallet = _wallet(name="small", current_cash=1.0, starting_capital=1000.0)
    agent = Agent(id=uuid.uuid4(), wallet_id=wallet.id, status="dead", mode="paper")
    agent.wallet = wallet
    session.add(agent)
    session.add(ControlCommand(id=uuid.uuid4(), wallet_id=wallet.id, command="reset_wallet", payload={}, status="applied"))

    cycle._apply_pending_resets(session)

    assert wallet.current_cash == 1.0  # untouched -- that command was already applied
    assert agent.status == "dead"


# --- live wallet branching ---


def test_run_cycle_scopes_a_live_wallet_to_live_settings_and_approved_strategies(monkeypatch):
    session = FakeSession()
    live_wallet = _wallet(name="live", kind="live")
    live_agent = Agent(id=uuid.uuid4(), wallet_id=live_wallet.id, status="alive", mode="live")
    live_agent.wallet = live_wallet
    session.add(live_agent)
    session.add(MarketCache(pair="I-BTC_INR", min_quantity=0, max_quantity=1, step=0.0001, min_notional=1,
                             base_precision=2, target_precision=4, raw={"symbol": "BTCINR"}))
    session.add(MarketCache(pair="I-ETH_INR", min_quantity=0, max_quantity=1, step=0.0001, min_notional=1,
                             base_precision=2, target_precision=4, raw={"symbol": "ETHINR"}))
    session.add(Setting(key="watchlist", value=["BTCINR", "ETHINR"]))
    session.add(Setting(key="watchlist_live", value=["BTCINR"]))
    session.add(Setting(key="risk", value={"profile": "paper"}))
    session.add(Setting(key="risk_live", value={"profile": "live"}))
    session.add(Strategy(id=uuid.uuid4(), type="ema_crossover", status="draft"))  # paper-only candidate
    session.add(Strategy(id=uuid.uuid4(), type="rsi_mean_reversion", status="approved_for_live"))

    monkeypatch.setattr(cycle, "get_session", lambda: _session_ctx(session))
    monkeypatch.setattr(cycle, "acquire_tick_lock", lambda session, holder: True)
    monkeypatch.setattr(cycle, "get_llm", lambda *a, **kw: object())
    monkeypatch.setattr(cycle, "_fetch_btc_price", lambda: None)
    monkeypatch.setattr(cycle, "is_dead", lambda *a, **kw: False)

    captured = {}

    def fake_run_wallet_tick(session, wallet, agent, pair_map, market_cache, strategies, risk, costs,
                              global_settings, live_trading_enabled, llm, llm_settings, death_threshold_pct, candle_interval, llm_fallback, **rent):
        captured["strategies"] = set(strategies.keys())
        captured["risk"] = risk
        captured["watchlist"] = sorted(pair_map.keys())
        return {"wallet": wallet.name, "equity": 1000.0, "prices": {}, "results": []}

    monkeypatch.setattr(cycle, "_run_wallet_tick", fake_run_wallet_tick)

    cycle.run_cycle()

    assert captured["strategies"] == {"rsi_mean_reversion"}  # draft excluded -- only approved_for_live/live
    assert captured["risk"] == {"profile": "live"}
    assert captured["watchlist"] == ["BTCINR"]


def test_run_cycle_kills_a_dead_live_wallet_via_live_engine(monkeypatch):
    session = FakeSession()
    live_wallet = _wallet(name="live", kind="live")
    live_agent = Agent(id=uuid.uuid4(), wallet_id=live_wallet.id, status="alive", mode="live")
    live_agent.wallet = live_wallet
    session.add(live_agent)

    monkeypatch.setattr(cycle, "get_session", lambda: _session_ctx(session))
    monkeypatch.setattr(cycle, "acquire_tick_lock", lambda session, holder: True)
    monkeypatch.setattr(cycle, "get_llm", lambda *a, **kw: object())
    monkeypatch.setattr(cycle, "_fetch_btc_price", lambda: None)
    monkeypatch.setattr(cycle, "is_dead", lambda *a, **kw: True)
    monkeypatch.setattr(cycle, "_run_wallet_tick",
                         lambda *a, **kw: {"wallet": "live", "equity": 1.0, "prices": {}, "results": []})

    paper_kill_calls, live_kill_calls = [], []
    monkeypatch.setattr(cycle, "kill_wallet", lambda *a, **kw: paper_kill_calls.append(1))
    monkeypatch.setattr(cycle.live_engine, "kill_wallet", lambda *a, **kw: live_kill_calls.append(1))

    cycle.run_cycle()

    assert live_kill_calls == [1]
    assert paper_kill_calls == []


def test_run_cycle_alerts_on_an_unresolved_live_order(monkeypatch):
    session = FakeSession()
    live_wallet = _wallet(name="live", kind="live")
    live_agent = Agent(id=uuid.uuid4(), wallet_id=live_wallet.id, status="alive", mode="live")
    live_agent.wallet = live_wallet
    session.add(live_agent)
    session.add(LiveOrder(wallet_id=live_wallet.id, pair="BTCINR", side="buy", client_order_id="c1",
                           status="unknown_needs_manual_check", requested_qty=0.001))

    monkeypatch.setattr(cycle, "get_session", lambda: _session_ctx(session))
    monkeypatch.setattr(cycle, "acquire_tick_lock", lambda session, holder: True)
    monkeypatch.setattr(cycle, "get_llm", lambda *a, **kw: object())
    monkeypatch.setattr(cycle, "_fetch_btc_price", lambda: None)
    monkeypatch.setattr(cycle, "is_dead", lambda *a, **kw: False)
    monkeypatch.setattr(cycle, "_run_wallet_tick",
                         lambda *a, **kw: {"wallet": "live", "equity": 1000.0, "prices": {}, "results": []})

    alerts = []
    monkeypatch.setattr(cycle, "maybe_send_alert", lambda session, alert_type, settings_map, text, **kw: alerts.append(alert_type))

    cycle.run_cycle()

    assert "live_order_unknown_state" in alerts


# --- run_cycle orchestration ---


def test_run_cycle_returns_skipped_when_lock_is_held(monkeypatch):
    monkeypatch.setattr(cycle, "get_session", lambda: _session_ctx(FakeSession()))
    monkeypatch.setattr(cycle, "acquire_tick_lock", lambda session, holder: False)

    result = cycle.run_cycle()

    assert result == {"skipped": "tick lock held by another run"}


def test_run_cycle_does_not_double_write_benchmarks_on_the_first_tick(monkeypatch):
    # Caught live (Phase 5 report): a wallet's very first tick both
    # initializes AND recorded a benchmark point in the same pass,
    # writing 4 equity_history rows instead of 2. Fixed by skipping the
    # record_benchmark_tick call on the tick that just initialized.
    session = FakeSession()
    wallet = _wallet(name="small")
    agent = Agent(id=uuid.uuid4(), wallet_id=wallet.id, status="alive", mode="paper")
    agent.wallet = wallet
    session.add(agent)

    monkeypatch.setattr(cycle, "get_session", lambda: _session_ctx(session))
    monkeypatch.setattr(cycle, "acquire_tick_lock", lambda session, holder: True)
    monkeypatch.setattr(cycle, "get_llm", lambda *a, **kw: object())
    monkeypatch.setattr(cycle, "_fetch_btc_price", lambda: 8000000.0)
    monkeypatch.setattr(cycle, "is_dead", lambda *a, **kw: False)
    monkeypatch.setattr(cycle, "_run_wallet_tick",
                         lambda *a, **kw: {"wallet": a[1].name, "equity": 1000.0, "prices": {}, "results": []})

    initialized, recorded = [], []
    monkeypatch.setattr(cycle, "initialize_benchmarks", lambda *a, **kw: initialized.append(1))
    monkeypatch.setattr(cycle, "record_benchmark_tick", lambda *a, **kw: recorded.append(1))

    cycle.run_cycle()

    assert initialized == [1]
    assert recorded == []  # not called on the same tick that just initialized


def test_run_cycle_ticks_every_alive_agent_and_beats_heartbeat(monkeypatch):
    session = FakeSession()
    alive_wallet = _wallet(name="small")
    alive_agent = Agent(id=uuid.uuid4(), wallet_id=alive_wallet.id, status="alive", mode="paper")
    alive_agent.wallet = alive_wallet
    dead_wallet = _wallet(name="large")
    dead_agent = Agent(id=uuid.uuid4(), wallet_id=dead_wallet.id, status="dead", mode="paper")
    dead_agent.wallet = dead_wallet
    session.add(alive_agent)
    session.add(dead_agent)

    monkeypatch.setattr(cycle, "get_session", lambda: _session_ctx(session))
    monkeypatch.setattr(cycle, "acquire_tick_lock", lambda session, holder: True)
    monkeypatch.setattr(cycle, "get_llm", lambda *a, **kw: object())
    monkeypatch.setattr(cycle, "_fetch_btc_price", lambda: None)  # skip benchmark init/record entirely
    monkeypatch.setattr(cycle, "is_dead", lambda *a, **kw: False)

    ticked = []
    monkeypatch.setattr(cycle, "_run_wallet_tick", lambda *a, **kw: (ticked.append(a[1].name), {"wallet": a[1].name, "equity": 1000.0, "prices": {}, "results": []})[1])

    result = cycle.run_cycle()

    assert ticked == ["small"]  # only the alive agent's wallet got ticked
    assert result["wallets"] == [{"wallet": "small", "equity": 1000.0, "prices": {}, "results": []}]
    heartbeats = [h for h in session.added if isinstance(h, Heartbeat)]
    assert len(heartbeats) == 1 and heartbeats[0].detail == {"wallets": 1}


@contextmanager
def _session_ctx(session):
    yield session


def test_run_cycle_keeps_a_held_pair_that_fell_out_of_the_auto_watchlist(monkeypatch):
    session = FakeSession()
    wallet = _wallet()
    agent = Agent(id=uuid.uuid4(), wallet_id=wallet.id, status="alive", mode="paper")
    agent.wallet = wallet
    session.add(agent)
    for symbol in ("BTCINR", "XRPINR"):
        session.add(MarketCache(pair=f"I-{symbol[:-3]}_INR", min_quantity=0, max_quantity=1, step=0.0001, min_notional=1,
                                 base_precision=2, target_precision=4, raw={"symbol": symbol}))
    session.add(Position(id=uuid.uuid4(), wallet_id=wallet.id, pair="XRPINR", side="buy", qty=1.0,
                         entry_price=100.0, stop_loss=95.0, take_profit=110.0, closed_at=None))
    monkeypatch.setattr(cycle.coindcx_client, "get_ticker", lambda: [
        {"market": "BTCINR", "volume": "40000000", "last_price": "100", "bid": "99.9", "ask": "100", "change_24_hour": "1"},
    ])
    monkeypatch.setattr(cycle, "get_session", lambda: _session_ctx(session))
    monkeypatch.setattr(cycle, "acquire_tick_lock", lambda session, holder: True)
    monkeypatch.setattr(cycle, "get_llm", lambda *a, **kw: object())
    monkeypatch.setattr(cycle, "_fetch_btc_price", lambda: None)
    monkeypatch.setattr(cycle, "is_dead", lambda *a, **kw: False)
    captured = {}

    def fake_run_wallet_tick(session, wallet, agent, pair_map, *rest, **kw):
        captured["watchlist"] = sorted(pair_map.keys())
        return {"wallet": wallet.name, "equity": 1000.0, "prices": {}, "results": []}

    monkeypatch.setattr(cycle, "_run_wallet_tick", fake_run_wallet_tick)

    cycle.run_cycle()

    assert captured["watchlist"] == ["BTCINR", "XRPINR"]  # top-N pick + the pair it still holds


def test_fallback_llm_defaults_to_gpt_oss_on_nvidia_and_can_be_disabled(monkeypatch):
    monkeypatch.setattr(cycle, "get_llm", lambda provider, model, keys: f"llm:{model}")

    assert cycle._fallback_llm({"provider": "nvidia", "model": "big"}) == ("llm:openai/gpt-oss-20b", "nvidia", "openai/gpt-oss-20b")
    assert cycle._fallback_llm({"provider": "nvidia", "model": "big", "fallback_model": ""}) is None
    assert cycle._fallback_llm({"provider": "anthropic", "model": "claude"}) is None
