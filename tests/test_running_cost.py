"""Daily running cost ("rent") -- the ledger itself, plus every place it
must bite: risk-sizing equity, the death check, the benchmarks, the
prompt, and a wallet reset. No real DB/network (FakeSession)."""

from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date

from core.benchmarks import record_benchmark_tick
from core.db.models import Agent, ControlCommand, EquityHistory, Setting, Wallet
from core.running_cost import accrue_rent, daily_cost, rent_paid, runway_days
from tests.conftest import FakeSession
from worker import cycle
from worker.agent import nodes

D1 = date(2026, 10, 4)


def _wallet(kind="paper", capital=10000.0):
    return Wallet(id=uuid.uuid4(), name=f"w-{kind}", kind=kind, starting_capital=capital, current_cash=capital, tds_credit=0.0)


# --- ledger ---


def test_first_sight_charges_today_then_same_day_charges_nothing():
    session, wallet = FakeSession(), _wallet()
    assert accrue_rent(session, wallet, 50, D1) == 50
    assert accrue_rent(session, wallet, 50, D1) == 50
    assert rent_paid(session, wallet.id) == 50


def test_missed_days_are_caught_up():
    session, wallet = FakeSession(), _wallet()
    accrue_rent(session, wallet, 50, D1)
    assert accrue_rent(session, wallet, 50, date(2026, 10, 7)) == 200  # day 1 + 3 missed days


def test_rent_never_touches_cash():
    session, wallet = FakeSession(), _wallet()
    accrue_rent(session, wallet, 50, D1)
    assert wallet.current_cash == 10000.0  # live cash mirrors the exchange -- a ledger, not a deduction


def test_paper_and_live_daily_cost_defaults_and_overrides():
    assert daily_cost({}, "paper") == 50 and daily_cost({}, "live") == 5
    assert daily_cost({"live_daily_inr": 20}, "live") == 20


def test_runway_days():
    assert runway_days(10000, 2000, 50) == 160
    assert runway_days(1900, 2000, 50) == 0


# --- where it bites ---


def test_check_exits_equity_is_net_of_rent():
    wallet = _wallet()
    state = {"session": FakeSession(), "wallet": wallet, "costs_settings": {}, "open_positions": [],
             "candles": {}, "prices": {}, "rent_paid_inr": 350.0}
    assert nodes.check_exits(state)["equity"] == 10000.0 - 350.0


def test_survival_brief_tells_the_bot_about_rent_and_runway():
    wallet = _wallet()
    brief = nodes._survival_brief({"wallet": wallet, "equity": 9650.0, "death_threshold_pct": 20,
                                   "daily_cost_inr": 50.0, "rent_paid_inr": 350.0})
    assert "you pay 50.00 INR every day just to stay alive (350.00 INR paid so far" in brief
    assert "you die in about 153 days" in brief  # (9650 - 2000) // 50
    assert "must earn MORE than 50.00 INR per day" in brief


def test_benchmarks_pay_the_same_rent():
    session, wallet = FakeSession(), _wallet()
    session.add(Setting(key=f"benchmark_btc:{wallet.id}", value={"btc_qty": 0.001}))
    record_benchmark_tick(session, wallet, 8_000_000.0, rent_paid_inr=100.0)
    by_series = {h.series: h.equity_inr for h in session.query(EquityHistory).all()}
    assert by_series == {"benchmark_btc": 8000.0 - 100.0, "benchmark_cash": 10000.0 - 100.0}


def test_rent_alone_can_kill_a_wallet(monkeypatch):
    session = FakeSession()
    wallet = _wallet(capital=1000.0)
    agent = Agent(id=uuid.uuid4(), wallet_id=wallet.id, status="alive", mode="paper")
    agent.wallet = wallet
    session.add(agent)
    session.add(Setting(key="death_threshold_pct", value=20))
    # 1000 capital, nothing traded, but 801 INR of rent already owed -> effective equity 199 < 200 death line
    session.add(Setting(key=f"running_cost:{wallet.id}", value={"charged_through": "2099-01-01", "total_inr": 801.0}))

    @contextmanager
    def ctx():
        yield session

    monkeypatch.setattr(cycle, "get_session", ctx)
    monkeypatch.setattr(cycle, "acquire_tick_lock", lambda session, holder: True)
    monkeypatch.setattr(cycle, "release_lock", lambda session, holder: None)
    monkeypatch.setattr(cycle, "research_due", lambda session, every_hours: False)
    monkeypatch.setattr(cycle.coindcx_client, "get_ticker", lambda: [])
    monkeypatch.setattr(cycle, "get_llm", lambda *a, **kw: object())
    monkeypatch.setattr(cycle, "_fetch_btc_price", lambda: None)
    monkeypatch.setattr(cycle, "_run_wallet_tick", lambda *a, **kw: {"wallet": wallet.name, "equity": 0, "prices": {}, "results": []})
    killed = []
    monkeypatch.setattr(cycle, "kill_wallet", lambda *a, **kw: killed.append(wallet.name))
    monkeypatch.setattr(cycle, "maybe_send_alert", lambda *a, **kw: None)

    cycle.run_cycle()

    assert killed == [wallet.name]
    (agent_point,) = [h for h in session.query(EquityHistory).all() if h.series == "agent"]
    assert agent_point.equity_inr == 199.0


def test_reset_clears_rent_and_can_set_a_new_starting_capital():
    session = FakeSession()
    wallet = _wallet(capital=1000.0)
    agent = Agent(id=uuid.uuid4(), wallet_id=wallet.id, status="dead", mode="paper")
    agent.wallet = wallet
    session.add(agent)
    session.add(Setting(key=f"running_cost:{wallet.id}", value={"charged_through": "2026-10-01", "total_inr": 150.0}))
    session.add(ControlCommand(id=uuid.uuid4(), wallet_id=wallet.id, command="reset_wallet",
                               payload={"starting_capital": 10000}, status="pending"))

    cycle._apply_pending_resets(session)

    assert rent_paid(session, wallet.id) == 0.0
    assert wallet.starting_capital == 10000.0 and wallet.current_cash == 10000.0
    assert agent.status == "alive"
