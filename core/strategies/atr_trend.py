from __future__ import annotations

from pydantic import Field

from core.strategies.base import StrategyParams, StrategySignal
from core.strategies.indicators import atr, sma


class Params(StrategyParams):
    trend_period: int = Field(20, ge=10, le=100)  # SMA defining trend direction
    atr_period: int = Field(14, ge=5, le=30)
    atr_stop_multiple: float = Field(2.0, ge=1.0, le=5.0)
    atr_target_multiple: float = Field(3.0, ge=1.0, le=10.0)


def generate_signal(candles: list[dict], params: Params, has_position: bool) -> StrategySignal:
    closes = [c["close"] for c in candles]
    trend = sma(closes, params.trend_period)
    atr_value = atr(candles, params.atr_period)
    if trend is None or atr_value is None:
        return StrategySignal(action="hold", reasoning="not enough candle history")

    price = closes[-1]
    if not has_position and price > trend:
        return StrategySignal(
            action="buy",
            stop_loss=price - params.atr_stop_multiple * atr_value,
            take_profit=price + params.atr_target_multiple * atr_value,
            reasoning=f"price {price:.4f} above {params.trend_period}-SMA {trend:.4f} -- ATR-sized stop/target",
        )
    if has_position and price < trend:
        return StrategySignal(action="sell", reasoning=f"price {price:.4f} fell below {params.trend_period}-SMA {trend:.4f}")
    return StrategySignal(action="hold", reasoning="no new trend signal")
