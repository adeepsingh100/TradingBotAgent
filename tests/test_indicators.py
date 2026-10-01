from __future__ import annotations

import pytest

from core.strategies.indicators import atr, bollinger_bands, ema_series, rsi, sma


def test_sma_needs_full_period():
    assert sma([1, 2], 3) is None
    assert sma([1, 2, 3], 3) == pytest.approx(2.0)
    assert sma([1, 2, 3, 4], 3) == pytest.approx(3.0)  # last 3 only


def test_ema_series_seeded_with_sma_then_smoothed():
    values = [1, 2, 3, 4, 5, 6]
    series = ema_series(values, 3)
    assert series[0] == pytest.approx(2.0)  # SMA of [1,2,3]
    assert len(series) == len(values) - 3 + 1
    # each next point is strictly between the previous EMA and the new price for a rising series
    for i in range(1, len(series)):
        assert series[i] > series[i - 1]


def test_rsi_all_gains_is_100():
    values = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15]
    assert rsi(values, 14) == pytest.approx(100.0)


def test_rsi_all_losses_is_0():
    values = list(range(15, 0, -1))
    assert rsi(values, 14) == pytest.approx(0.0)


def test_rsi_needs_period_plus_one_values():
    assert rsi([1.0] * 10, 14) is None


def test_bollinger_bands_mid_equals_mean_and_bands_symmetric():
    values = [10.0] * 19 + [20.0]
    bands = bollinger_bands(values, period=20, num_std=2.0)
    lower, mid, upper = bands
    assert mid - lower == pytest.approx(upper - mid)
    assert lower < mid < upper


def test_atr_needs_period_plus_one_candles():
    candles = [{"high": 10, "low": 9, "close": 9.5}] * 10
    assert atr(candles, 14) is None


def test_atr_computes_true_range_average():
    candles = [
        {"high": 10, "low": 8, "close": 9},
        {"high": 11, "low": 9, "close": 10},
        {"high": 12, "low": 10, "close": 11},
    ]
    value = atr(candles, period=2)
    assert value is not None and value > 0
