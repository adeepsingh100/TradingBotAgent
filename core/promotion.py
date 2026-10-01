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
from core.db.models import EquityHistory, Trade, Wallet


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


def gather_live_promotion_stats(session, strategy_id) -> dict | None:
    """Aggregates one strategy's track record across every PAPER wallet
    that has ever traded it -- deliberately NOT scoped to a single
    wallet, matching core/db/models.py's own design note that
    strategies are global specifically so promotion evidence
    accumulates across every wallet that tries one, rather than each
    wallet re-proving it from scratch. Returns None if there's no
    closed-trade history yet (nothing for clears_live_promotion to
    evaluate) -- `stats["benchmark_return_pct"]` is bundled in here too
    (pop it off before passing the rest to clears_live_promotion) since
    computing it needs the same trade window this function already
    has in hand.

    `max_drawdown_pct`/`return_pct` use the combined starting_capital
    of every contributing wallet as the capital base, tracking
    cumulative PnL's peak-to-trough as a % of that base -- an
    approximation when trades span multiple wallets with different
    capital (there's no single coherent "equity curve" across
    wallets), not an exact replay. `benchmark_return_pct` uses the most
    recently contributing wallet's own benchmark_btc equity_history
    over the trade window as the representative comparison, same
    reasoning -- this feeds a human-reviewed approval button, not an
    automatic gate, so an honest approximation here is an acceptable
    trade-off against the complexity of a fully cross-wallet-exact
    calculation.
    """
    paper_wallet_ids = {w.id for w in session.query(Wallet).all() if w.kind == "paper"}
    trades = [
        t for t in session.query(Trade).filter_by(strategy_id=strategy_id).all()
        if t.pnl is not None and t.wallet_id in paper_wallet_ids
    ]
    if not trades:
        return None
    trades.sort(key=lambda t: t.executed_at)

    net_pnl = sum(t.pnl for t in trades)
    wins = sum(t.pnl for t in trades if t.pnl > 0)
    losses = sum(-t.pnl for t in trades if t.pnl < 0)
    profit_factor = (wins / losses) if losses > 0 else float("inf")
    days_tested = (trades[-1].executed_at - trades[0].executed_at).total_seconds() / 86400

    contributing_wallet_ids = {t.wallet_id for t in trades}
    capital_base = sum(w.starting_capital for w in session.query(Wallet).all() if w.id in contributing_wallet_ids)

    cum_pnl, peak, max_drawdown_pct = 0.0, 0.0, 0.0
    for t in trades:
        cum_pnl += t.pnl
        peak = max(peak, cum_pnl)
        if capital_base > 0:
            max_drawdown_pct = max(max_drawdown_pct, (peak - cum_pnl) / capital_base * 100)
    return_pct = (net_pnl / capital_base * 100) if capital_base > 0 else 0.0

    benchmark_points = sorted(
        (
            h for h in session.query(EquityHistory).filter_by(
                wallet_id=trades[-1].wallet_id, series="benchmark_btc"
            ).all()
            if trades[0].executed_at <= h.recorded_at <= trades[-1].executed_at
        ),
        key=lambda h: h.recorded_at,
    )
    benchmark_return_pct = 0.0
    if len(benchmark_points) >= 2 and benchmark_points[0].equity_inr > 0:
        benchmark_return_pct = (
            (benchmark_points[-1].equity_inr - benchmark_points[0].equity_inr) / benchmark_points[0].equity_inr * 100
        )

    return {
        "trade_count": len(trades), "days_tested": days_tested, "net_pnl": net_pnl,
        "profit_factor": profit_factor, "max_drawdown_pct": max_drawdown_pct, "return_pct": return_pct,
        "benchmark_return_pct": benchmark_return_pct,
    }
