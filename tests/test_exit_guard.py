"""No real DB/network -- FakeSession plus monkeypatched ticker/lock/
session, so this is about the guard's own decisions: which positions
close, at what fill price, and that a held lock means no work."""

from __future__ import annotations

import uuid
from contextlib import contextmanager

from core.db.models import Setting, Trade, Wallet
from core.paper_engine import open_position
from tests.conftest import FakeSession
from worker import exit_guard

COSTS = {"maker_fee_pct": 0.2, "taker_fee_pct": 0.2, "tds_pct": 1.0, "slippage_pct": 0.0}


class _Session(FakeSession):
    def execute(self, *a, **kw):  # SET LOCAL lock_timeout
        return None


def _setup(monkeypatch, ticker_price, lock_free=True):
    session = _Session()
    wallet = Wallet(id=uuid.uuid4(), name="small", kind="paper", starting_capital=1000.0, current_cash=1000.0, tds_credit=0.0)
    session.add(wallet)
    session.add(Setting(key="costs", value=COSTS))
    position = open_position(session, wallet, pair="BTCINR", qty=1.0, entry_price=100.0,
                             stop_loss=95.0, take_profit=110.0, costs_settings=COSTS)

    @contextmanager
    def ctx():
        yield session

    monkeypatch.setattr(exit_guard, "get_session", ctx)
    monkeypatch.setattr(exit_guard, "acquire_tick_lock", lambda *a, **kw: lock_free)
    monkeypatch.setattr(exit_guard, "release_lock", lambda *a, **kw: None)
    monkeypatch.setattr(exit_guard.coindcx_client, "get_ticker", lambda: [{"market": "BTCINR", "last_price": str(ticker_price)}])
    return session, position


def _exit_trade(session):
    return next(t for t in session.query(Trade).all() if t.side == "sell")


def test_stop_gapped_through_fills_at_the_worse_observed_price(monkeypatch):
    session, position = _setup(monkeypatch, ticker_price=90.0)

    result = exit_guard.run_exit_pass()

    assert result["closed"][0]["reason"] == "stop_loss"
    assert position.closed_at is not None
    assert _exit_trade(session).price == 90.0  # not the 95.0 stop level


def test_target_fills_at_the_target_level_never_better(monkeypatch):
    session, position = _setup(monkeypatch, ticker_price=130.0)

    exit_guard.run_exit_pass()

    assert _exit_trade(session).price == 110.0


def test_price_between_stop_and_target_leaves_the_position_open(monkeypatch):
    session, position = _setup(monkeypatch, ticker_price=101.0)

    assert exit_guard.run_exit_pass()["closed"] == []
    assert position.closed_at is None


def test_held_lock_means_no_work(monkeypatch):
    session, position = _setup(monkeypatch, ticker_price=90.0, lock_free=False)

    assert exit_guard.run_exit_pass() == {"skipped": "exit lock held"}
    assert position.closed_at is None


def test_start_exit_guard_disabled_at_zero():
    assert exit_guard.start_exit_guard(0) is None
