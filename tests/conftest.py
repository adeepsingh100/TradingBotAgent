"""Shared test helper -- not a DB/network fixture (this repo has none
of those in its suite), just a convenience for building the candle
dicts core/coindcx/client.py::get_candles returns (verified live field
names: open/high/low/close/volume/time)."""

from __future__ import annotations


def make_candles(closes: list[float], volumes: list[float] | None = None) -> list[dict]:
    volumes = volumes or [1.0] * len(closes)
    candles = []
    prev_close = closes[0]
    for i, (close, volume) in enumerate(zip(closes, volumes)):
        candles.append({
            "open": prev_close,
            "high": max(prev_close, close) + 0.01,
            "low": min(prev_close, close) - 0.01,
            "close": close,
            "volume": volume,
            "time": i,
        })
        prev_close = close
    return candles
