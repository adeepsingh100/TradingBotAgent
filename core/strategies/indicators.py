"""Pure, stdlib-only indicator math -- no numpy/scipy anywhere in this
repo (matches this project's own small-and-auditable bar: every
strategy's logic should be readable end to end, not hidden inside a
vectorized library call). All functions return None when there isn't
enough history yet, rather than raising -- a strategy facing a cold
start should get a clean "hold", not a crash.
"""

from __future__ import annotations


def sma(values: list[float], period: int) -> float | None:
    if len(values) < period:
        return None
    return sum(values[-period:]) / period


def ema_series(values: list[float], period: int) -> list[float]:
    """Full EMA series, seeded with an SMA over the first `period`
    values -- a crossover strategy needs at least two consecutive
    points to detect a cross, not just the latest value."""
    if len(values) < period:
        return []
    k = 2 / (period + 1)
    result = [sum(values[:period]) / period]
    for price in values[period:]:
        result.append(price * k + result[-1] * (1 - k))
    return result


def rsi(values: list[float], period: int = 14) -> float | None:
    if len(values) < period + 1:
        return None
    gains, losses = [], []
    for i in range(1, period + 1):
        change = values[-i] - values[-i - 1]
        (gains if change > 0 else losses).append(abs(change))
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    if avg_loss == 0:
        return 100.0
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))


def bollinger_bands(values: list[float], period: int = 20, num_std: float = 2.0) -> tuple[float, float, float] | None:
    """Returns (lower, mid, upper)."""
    if len(values) < period:
        return None
    window = values[-period:]
    mid = sum(window) / period
    variance = sum((v - mid) ** 2 for v in window) / period
    std = variance**0.5
    return mid - num_std * std, mid, mid + num_std * std


def atr(candles: list[dict], period: int = 14) -> float | None:
    """candles are oldest-first dicts with open/high/low/close/volume/time
    (the shape core/coindcx/client.py::get_candles returns)."""
    if len(candles) < period + 1:
        return None
    true_ranges = []
    for i in range(1, period + 1):
        candle = candles[-i]
        prev_close = candles[-i - 1]["close"]
        true_ranges.append(max(
            candle["high"] - candle["low"],
            abs(candle["high"] - prev_close),
            abs(candle["low"] - prev_close),
        ))
    return sum(true_ranges) / period
