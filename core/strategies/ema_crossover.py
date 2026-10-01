from __future__ import annotations

from pydantic import Field

from core.strategies.base import StrategyParams, StrategySignal
from core.strategies.indicators import ema_series


class Params(StrategyParams):
    fast_period: int = Field(12, ge=5, le=50)
    slow_period: int = Field(26, ge=10, le=200)
    stop_loss_pct: float = Field(2.0, ge=0.5, le=10.0)
    take_profit_pct: float = Field(4.0, ge=1.0, le=20.0)


def generate_signal(candles: list[dict], params: Params, has_position: bool) -> StrategySignal:
    closes = [c["close"] for c in candles]
    fast = ema_series(closes, params.fast_period)
    slow = ema_series(closes, params.slow_period)
    n = min(len(fast), len(slow))
    if n < 2:
        return StrategySignal(action="hold", reasoning="not enough candle history for both EMAs yet")

    fast, slow = fast[-n:], slow[-n:]
    crossed_up = fast[-2] <= slow[-2] and fast[-1] > slow[-1]
    crossed_down = fast[-2] >= slow[-2] and fast[-1] < slow[-1]
    price = closes[-1]

    if not has_position and crossed_up:
        return StrategySignal(
            action="buy",
            stop_loss=price * (1 - params.stop_loss_pct / 100),
            take_profit=price * (1 + params.take_profit_pct / 100),
            reasoning=f"EMA{params.fast_period} crossed above EMA{params.slow_period}",
        )
    if has_position and crossed_down:
        return StrategySignal(action="sell", reasoning=f"EMA{params.fast_period} crossed below EMA{params.slow_period}")
    return StrategySignal(action="hold", reasoning="no crossover")
