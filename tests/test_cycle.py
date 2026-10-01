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

from core.db.models import Agent, Heartbeat, MarketCache, Trade, Wallet
from tests.conftest import FakeSession
from worker import cycle

RISK = {"consecutive_losses_trigger": 2, "cooldown_hours_after_losses": 4}


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
