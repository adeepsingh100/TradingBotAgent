from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone

import pytest

from core.backtester import BacktestResult, BacktestTrade
from core.db.models import Trade, Wallet
from core.promotion import clears_backtest_bar, clears_live_promotion, gather_live_promotion_stats
from tests.conftest import FakeSession

BACKTEST_BAR = {"min_trades": 5, "min_net_pnl": 0, "min_profit_factor": 1.0, "max_drawdown_pct": 25}
LIVE_CRITERIA = {"min_trades": 20, "min_days": 14, "min_profit_factor": 1.2, "max_drawdown_pct": 15}


def _result(trade_count=10, net_pnl=100.0, profit_factor=1.5, max_drawdown_pct=10.0) -> BacktestResult:
    trades = [BacktestTrade(0, 1, 100, 110, 1, net_pnl / max(trade_count, 1), "take_profit")] * trade_count
    return BacktestResult(trades=trades, net_pnl=net_pnl, win_rate=1.0, profit_factor=profit_factor, max_drawdown_pct=max_drawdown_pct)


def test_clears_backtest_bar_passes_a_solid_result():
    passed, reasons = clears_backtest_bar(_result(), BACKTEST_BAR)
    assert passed is True
    assert reasons == []


def test_clears_backtest_bar_rejects_too_few_trades():
    passed, reasons = clears_backtest_bar(_result(trade_count=2), BACKTEST_BAR)
    assert passed is False
    assert any("trades" in r for r in reasons)


def test_clears_backtest_bar_rejects_negative_pnl():
    passed, reasons = clears_backtest_bar(_result(net_pnl=-5.0), BACKTEST_BAR)
    assert passed is False
    assert any("net_pnl" in r for r in reasons)


def test_clears_backtest_bar_rejects_excess_drawdown():
    passed, reasons = clears_backtest_bar(_result(max_drawdown_pct=40.0), BACKTEST_BAR)
    assert passed is False
    assert any("drawdown" in r for r in reasons)


def _stats(**overrides) -> dict:
    defaults = dict(trade_count=25, days_tested=20, net_pnl=500.0, profit_factor=1.5, max_drawdown_pct=10.0, return_pct=8.0)
    defaults.update(overrides)
    return defaults


def test_clears_live_promotion_passes_when_everything_clears_and_beats_benchmark():
    passed, reasons = clears_live_promotion(_stats(), LIVE_CRITERIA, benchmark_return_pct=5.0)
    assert passed is True
    assert reasons == []


def test_clears_live_promotion_rejects_not_enough_days():
    passed, reasons = clears_live_promotion(_stats(days_tested=10), LIVE_CRITERIA, benchmark_return_pct=5.0)
    assert passed is False
    assert any("days" in r for r in reasons)


def test_clears_live_promotion_rejects_when_it_doesnt_beat_the_benchmark():
    passed, reasons = clears_live_promotion(_stats(return_pct=3.0), LIVE_CRITERIA, benchmark_return_pct=5.0)
    assert passed is False
    assert any("buy-and-hold" in r for r in reasons)


def test_clears_live_promotion_rejects_drawdown_at_the_boundary():
    # spec section 5: "max drawdown below 15%" -- 15.0 itself must fail, not just >15
    passed, reasons = clears_live_promotion(_stats(max_drawdown_pct=15.0), LIVE_CRITERIA, benchmark_return_pct=5.0)
    assert passed is False
    assert any("drawdown" in r for r in reasons)


def test_gather_live_promotion_stats_returns_none_without_closed_trades():
    assert gather_live_promotion_stats(FakeSession(), uuid.uuid4()) is None


def test_gather_live_promotion_stats_aggregates_paper_wallets_and_excludes_live_and_open_trades():
    session = FakeSession()
    strategy_id = uuid.uuid4()
    paper_1 = Wallet(id=uuid.uuid4(), name="small", kind="paper", starting_capital=1000.0, current_cash=1000.0)
    paper_2 = Wallet(id=uuid.uuid4(), name="large", kind="paper", starting_capital=2000.0, current_cash=2000.0)
    live = Wallet(id=uuid.uuid4(), name="live", kind="live", starting_capital=500.0, current_cash=500.0)
    for w in (paper_1, paper_2, live):
        session.add(w)

    now = datetime.now(timezone.utc)
    session.add(Trade(wallet_id=paper_1.id, strategy_id=strategy_id, pair="BTCINR", side="sell",
                       qty=1, price=1, fee=0, pnl=100.0, executed_at=now - timedelta(days=5)))
    session.add(Trade(wallet_id=paper_2.id, strategy_id=strategy_id, pair="BTCINR", side="sell",
                       qty=1, price=1, fee=0, pnl=-20.0, executed_at=now))
    session.add(Trade(wallet_id=live.id, strategy_id=strategy_id, pair="BTCINR", side="sell",
                       qty=1, price=1, fee=0, pnl=99999.0, executed_at=now))  # must be excluded -- not paper
    session.add(Trade(wallet_id=paper_1.id, strategy_id=strategy_id, pair="BTCINR", side="buy",
                       qty=1, price=1, fee=0, pnl=None, executed_at=now))  # open leg -- excluded, pnl is None

    stats = gather_live_promotion_stats(session, strategy_id)

    assert stats["trade_count"] == 2
    assert stats["net_pnl"] == pytest.approx(80.0)
    assert stats["days_tested"] == pytest.approx(5.0, abs=0.01)
    assert "benchmark_return_pct" in stats
