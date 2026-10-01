from __future__ import annotations

from core.backtester import BacktestResult, BacktestTrade
from core.promotion import clears_backtest_bar, clears_live_promotion

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
