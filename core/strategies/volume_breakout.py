"""No discretionary exit rule -- once in a position, this strategy
rides it until the mechanical stop/take-profit fires (paper_engine's
check_stop_or_target), unlike the other four. That's a deliberate
simplification for a volume-spike entry signal, not a bug: there's no
natural "volume says get out now" condition the way a crossover or
RSI threshold gives one.
"""

from __future__ import annotations

from pydantic import Field

from core.strategies.base import StrategyParams, StrategySignal


class Params(StrategyParams):
    lookback: int = Field(20, ge=10, le=50)
    volume_multiple: float = Field(2.0, ge=1.2, le=5.0)
    stop_loss_pct: float = Field(3.0, ge=0.5, le=10.0)
    take_profit_pct: float = Field(5.0, ge=1.0, le=20.0)


def generate_signal(candles: list[dict], params: Params, has_position: bool) -> StrategySignal:
    if has_position:
        return StrategySignal(action="hold", reasoning="no discretionary exit -- relies on stop/take-profit")

    if len(candles) < params.lookback + 1:
        return StrategySignal(action="hold", reasoning="not enough candle history")

    recent = candles[-(params.lookback + 1):-1]
    avg_volume = sum(c["volume"] for c in recent) / len(recent)
    current = candles[-1]
    price = current["close"]

    is_breakout = current["volume"] > avg_volume * params.volume_multiple and current["close"] > current["open"]
    if is_breakout:
        return StrategySignal(
            action="buy",
            stop_loss=price * (1 - params.stop_loss_pct / 100),
            take_profit=price * (1 + params.take_profit_pct / 100),
            reasoning=f"volume {current['volume']:.4f} > {params.volume_multiple}x avg {avg_volume:.4f} on an up candle",
        )
    return StrategySignal(action="hold", reasoning="no volume breakout")
