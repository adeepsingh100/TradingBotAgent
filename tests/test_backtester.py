from __future__ import annotations

from core.backtester import run_backtest
from tests.conftest import make_candles

COSTS = {"maker_fee_pct": 0.2, "taker_fee_pct": 0.2, "tds_pct": 1.0, "slippage_pct": 0.1}


def test_backtest_buys_low_sells_high_and_reports_a_net_profit():
    # Sharp decline (RSI -> oversold, buys near the bottom) then a
    # sharp rise (RSI -> overbought, sells near the top) -- one
    # unambiguous winning round-trip.
    closes = list(range(200, 99, -7)) + list(range(100, 210, 7))
    candles = make_candles([float(c) for c in closes])

    result = run_backtest(
        candles, "rsi_mean_reversion", {"period": 14},
        starting_capital=1000.0, costs_settings=COSTS, max_position_size_pct=20,
    )

    assert result.trade_count >= 1
    assert result.net_pnl > 0
    assert result.win_rate > 0
    assert 0.0 <= result.max_drawdown_pct <= 100.0


def test_backtest_never_opens_a_position_bigger_than_the_sizing_cap():
    closes = list(range(200, 99, -7)) + list(range(100, 210, 7))
    candles = make_candles([float(c) for c in closes])

    result = run_backtest(
        candles, "rsi_mean_reversion", {"period": 14},
        starting_capital=1000.0, costs_settings=COSTS, max_position_size_pct=20,
    )

    for trade in result.trades:
        assert trade.qty * trade.entry_price <= 1000.0 * 0.20 * 1.01  # small slack for the slippage-adjusted fill price


def test_backtest_with_no_signals_ever_trades_nothing():
    candles = make_candles([100.0] * 30)  # perfectly flat -- RSI stays neutral forever
    result = run_backtest(
        candles, "rsi_mean_reversion", {"period": 14},
        starting_capital=1000.0, costs_settings=COSTS, max_position_size_pct=20,
    )
    assert result.trade_count == 0
    assert result.net_pnl == 0.0
    assert result.profit_factor == 0.0


def test_backtest_respects_stop_loss_against_candle_low_not_just_close():
    # ema_crossover enters on the one-candle up-cross (same shape proven
    # in test_strategies.py), then the very next candle gaps down through
    # the stop on its LOW even though its CLOSE stays well above it --
    # the fill must still trigger, same honesty rule as the live paper
    # engine (spec section 6).
    candles = make_candles([100.0] * 15 + [105.0])
    candles.append({"open": 105.0, "high": 106.0, "low": 90.0, "close": 105.0, "volume": 1, "time": len(candles)})

    result = run_backtest(
        candles, "ema_crossover", {"fast_period": 5, "slow_period": 10, "stop_loss_pct": 2.0, "take_profit_pct": 20.0},
        starting_capital=1000.0, costs_settings=COSTS, max_position_size_pct=20,
    )

    assert len(result.trades) == 1
    assert result.trades[0].exit_reason == "stop_loss"
    assert result.trades[0].pnl < 0
