from __future__ import annotations

import pytest
from pydantic import ValidationError

from core.strategies import atr_trend, bollinger, ema_crossover, rsi_mean_reversion, volume_breakout
from core.strategies.registry import generate_signal, validate_params
from tests.conftest import make_candles


def _any_action(strategy_signals, action):
    return any(s.action == action for s in strategy_signals)


# --- ema_crossover ---


def test_ema_crossover_buys_on_an_upward_cross():
    params = ema_crossover.Params(fast_period=5, slow_period=10)
    candles = make_candles([100] * 15 + [105, 110, 115, 120, 125, 130, 135, 140, 145, 150])
    signals = [ema_crossover.generate_signal(candles[:i], params, has_position=False) for i in range(11, len(candles) + 1)]
    assert _any_action(signals, "buy")


def test_ema_crossover_holds_when_flat():
    params = ema_crossover.Params(fast_period=5, slow_period=10)
    candles = make_candles([100] * 20)
    assert ema_crossover.generate_signal(candles, params, has_position=False).action == "hold"


def test_ema_crossover_sells_on_a_downward_cross_while_in_position():
    params = ema_crossover.Params(fast_period=5, slow_period=10)
    candles = make_candles([100] * 15 + [95, 90, 85, 80, 75, 70, 65, 60, 55, 50])
    signals = [ema_crossover.generate_signal(candles[:i], params, has_position=True) for i in range(11, len(candles) + 1)]
    assert _any_action(signals, "sell")


# --- rsi_mean_reversion ---


def test_rsi_buys_when_oversold():
    params = rsi_mean_reversion.Params(period=14)
    candles = make_candles(list(range(130, 100, -2)))  # steadily declining -> RSI near 0
    signal = rsi_mean_reversion.generate_signal(candles, params, has_position=False)
    assert signal.action == "buy"
    assert signal.stop_loss < candles[-1]["close"] < signal.take_profit


def test_rsi_sells_when_overbought_while_in_position():
    params = rsi_mean_reversion.Params(period=14)
    candles = make_candles(list(range(100, 130, 2)))  # steadily rising -> RSI near 100
    signal = rsi_mean_reversion.generate_signal(candles, params, has_position=True)
    assert signal.action == "sell"


# --- bollinger ---


def test_bollinger_buys_on_a_dip_below_the_lower_band():
    params = bollinger.Params(period=20, num_std=2.0)
    candles = make_candles([100] * 19 + [80])  # sharp dip
    signal = bollinger.generate_signal(candles, params, has_position=False)
    assert signal.action == "buy"


def test_bollinger_sells_on_reversion_to_mid_while_in_position():
    params = bollinger.Params(period=20, num_std=2.0)
    candles = make_candles([100] * 20)  # flat -- price sits right at the mid band
    signal = bollinger.generate_signal(candles, params, has_position=True)
    assert signal.action == "sell"


# --- atr_trend ---


def test_atr_trend_buys_above_the_trend_sma():
    params = atr_trend.Params(trend_period=10, atr_period=5)
    candles = make_candles(list(range(100, 130)))  # rising -- last close well above its own SMA
    signal = atr_trend.generate_signal(candles, params, has_position=False)
    assert signal.action == "buy"
    assert signal.stop_loss < candles[-1]["close"] < signal.take_profit


def test_atr_trend_sells_below_the_trend_sma_while_in_position():
    params = atr_trend.Params(trend_period=10, atr_period=5)
    candles = make_candles(list(range(130, 100, -1)))  # falling
    signal = atr_trend.generate_signal(candles, params, has_position=True)
    assert signal.action == "sell"


# --- volume_breakout ---


def test_volume_breakout_buys_on_a_volume_spike_up_candle():
    params = volume_breakout.Params(lookback=10, volume_multiple=2.0)
    closes = [100.0] * 20 + [103.0]  # last candle closes up
    volumes = [1.0] * 20 + [5.0]  # and spikes volume
    candles = make_candles(closes, volumes)
    signal = volume_breakout.generate_signal(candles, params, has_position=False)
    assert signal.action == "buy"


def test_volume_breakout_never_proposes_a_discretionary_exit():
    params = volume_breakout.Params()
    candles = make_candles([100.0] * 20)
    assert volume_breakout.generate_signal(candles, params, has_position=True).action == "hold"


# --- registry ---


def test_validate_params_rejects_out_of_range_value():
    with pytest.raises(ValidationError):
        validate_params("ema_crossover", {"fast_period": 1})  # below ge=5


def test_validate_params_accepts_in_range_value():
    params = validate_params("ema_crossover", {"fast_period": 8})
    assert params.fast_period == 8


def test_validate_params_rejects_unknown_strategy_type():
    with pytest.raises(ValueError):
        validate_params("made_up_strategy", {})


def test_registry_generate_signal_dispatches_to_the_right_strategy():
    candles = make_candles([100] * 20)
    signal = generate_signal("rsi_mean_reversion", candles, {}, has_position=False)
    assert signal.action == "hold"  # flat price -> neutral RSI
