"""Shared shapes every strategy module implements against.

Each strategy file exports a `Params` class (subclassing `StrategyParams`,
with `Field(ge=..., le=...)` bounds -- those bounds ARE the "safe
min/max ranges" spec section 5 requires; the LLM, Phase 5, can only
construct a value Pydantic accepts) and a `generate_signal(candles,
params, has_position)` function. core/strategies/registry.py is the
only place that imports all five by name -- nothing else should import
a strategy module directly, so a new strategy only ever needs
registering in one place.
"""

from __future__ import annotations

from pydantic import BaseModel


class StrategyParams(BaseModel):
    pass


class StrategySignal(BaseModel):
    action: str  # buy | sell | hold
    stop_loss: float | None = None  # only meaningful on a "buy"
    take_profit: float | None = None  # only meaningful on a "buy"
    reasoning: str = ""
