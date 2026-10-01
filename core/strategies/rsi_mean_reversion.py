from __future__ import annotations

from pydantic import Field

from core.strategies.base import StrategyParams, StrategySignal
from core.strategies.indicators import rsi


class Params(StrategyParams):
    period: int = Field(14, ge=5, le=30)
    oversold: float = Field(30.0, ge=10.0, le=40.0)
    overbought: float = Field(70.0, ge=60.0, le=90.0)
    stop_loss_pct: float = Field(2.0, ge=0.5, le=10.0)
    take_profit_pct: float = Field(3.0, ge=1.0, le=20.0)


def generate_signal(candles: list[dict], params: Params, has_position: bool) -> StrategySignal:
    closes = [c["close"] for c in candles]
    value = rsi(closes, params.period)
    if value is None:
        return StrategySignal(action="hold", reasoning="not enough candle history")

    price = closes[-1]
    if not has_position and value <= params.oversold:
        return StrategySignal(
            action="buy",
            stop_loss=price * (1 - params.stop_loss_pct / 100),
            take_profit=price * (1 + params.take_profit_pct / 100),
            reasoning=f"RSI {value:.1f} <= oversold threshold {params.oversold}",
        )
    if has_position and value >= params.overbought:
        return StrategySignal(action="sell", reasoning=f"RSI {value:.1f} >= overbought threshold {params.overbought}")
    return StrategySignal(action="hold", reasoning=f"RSI {value:.1f} neutral")
