"""Two separate gates, deliberately not merged: `clears_backtest_bar`
decides draft -> backtested is worth trying in paper_testing at all
(a loose bar); `clears_live_promotion` is spec section 5's explicit
approved_for_live numbers (a strict bar). Passing the first says
nothing about the second.

Both return (passed, reasons) rather than raising or just a bool --
the dashboard's Strategies page needs the actual reasons to show next
to a REJECT, not just a red X.
"""

from __future__ import annotations

from core.backtester import BacktestResult


def clears_backtest_bar(result: BacktestResult, bar: dict) -> tuple[bool, list[str]]:
    reasons = []
    if result.trade_count < bar["min_trades"]:
        reasons.append(f"only {result.trade_count} trades, need {bar['min_trades']}")
    if result.net_pnl <= bar["min_net_pnl"]:
        reasons.append(f"net_pnl {result.net_pnl:.2f} doesn't clear {bar['min_net_pnl']}")
    if result.profit_factor < bar["min_profit_factor"]:
        reasons.append(f"profit_factor {result.profit_factor:.2f} below {bar['min_profit_factor']}")
    if result.max_drawdown_pct > bar["max_drawdown_pct"]:
        reasons.append(f"max_drawdown {result.max_drawdown_pct:.1f}% exceeds {bar['max_drawdown_pct']}%")
    return len(reasons) == 0, reasons


def clears_live_promotion(stats: dict, criteria: dict, benchmark_return_pct: float) -> tuple[bool, list[str]]:
    """`stats` is the paper-trading track record for one strategy+wallet:
    trade_count, days_tested, net_pnl, profit_factor, max_drawdown_pct,
    return_pct (the agent's own % return over the test window, same
    basis as `benchmark_return_pct` so they're comparable)."""
    reasons = []
    if stats["trade_count"] < criteria["min_trades"]:
        reasons.append(f"only {stats['trade_count']} paper trades, need {criteria['min_trades']}")
    if stats["days_tested"] < criteria["min_days"]:
        reasons.append(f"only {stats['days_tested']} days tested, need {criteria['min_days']}")
    if stats["net_pnl"] <= 0:
        reasons.append(f"net_pnl {stats['net_pnl']:.2f} is not positive")
    if stats["profit_factor"] < criteria["min_profit_factor"]:
        reasons.append(f"profit_factor {stats['profit_factor']:.2f} below {criteria['min_profit_factor']}")
    if stats["max_drawdown_pct"] >= criteria["max_drawdown_pct"]:
        reasons.append(f"max_drawdown {stats['max_drawdown_pct']:.1f}% at/above {criteria['max_drawdown_pct']}%")
    if stats["return_pct"] <= benchmark_return_pct:
        reasons.append(f"return {stats['return_pct']:.2f}% doesn't beat BTC buy-and-hold {benchmark_return_pct:.2f}%")
    return len(reasons) == 0, reasons
