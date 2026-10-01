"""Mean-reversion variant specifically (spec section 5 says "breakout /
reversion", leaving the choice open) -- reversion fits this project's
"trades rarely, capital preservation" ethos better than breakout, and
pairs as a natural confirming/alternative signal alongside RSI mean
reversion. A breakout variant (buy when price closes above the upper
band on rising volume) is a plausible future addition, not built here.
"""

from __future__ import annotations

from pydantic import Field

from core.strategies.base import StrategyParams, StrategySignal
from core.strategies.indicators import bollinger_bands


class Params(StrategyParams):
    period: int = Field(20, ge=10, le=50)
    num_std: float = Field(2.0, ge=1.0, le=3.0)
    stop_loss_pct: float = Field(2.0, ge=0.5, le=10.0)


def generate_signal(candles: list[dict], params: Params, has_position: bool) -> StrategySignal:
    closes = [c["close"] for c in candles]
    bands = bollinger_bands(closes, params.period, params.num_std)
    if bands is None:
        return StrategySignal(action="hold", reasoning="not enough candle history")

    lower, mid, upper = bands
    price = closes[-1]
    if not has_position and price <= lower:
        return StrategySignal(
            action="buy",
            stop_loss=price * (1 - params.stop_loss_pct / 100),
            take_profit=mid,  # target is the band's own mean, not a fixed %
            reasoning=f"price {price:.4f} at/below lower band {lower:.4f}",
        )
    if has_position and price >= mid:
        return StrategySignal(action="sell", reasoning=f"price {price:.4f} reverted to/above mid band {mid:.4f}")
    return StrategySignal(action="hold", reasoning="price within bands")
