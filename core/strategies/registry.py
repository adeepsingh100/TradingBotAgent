"""The only module that imports all five strategy files by name --
adding a sixth strategy means adding one entry here, nothing else.
Also the enforcement point for spec section 5's "safe min/max ranges":
`validate_params` constructing the strategy's Pydantic `Params` class
is what rejects an out-of-range value the LLM (Phase 5) tries to tune
a param to.
"""

from __future__ import annotations

from core.strategies import atr_trend, bollinger, ema_crossover, rsi_mean_reversion, volume_breakout
from core.strategies.base import StrategyParams, StrategySignal

STRATEGY_REGISTRY: dict[str, tuple[type[StrategyParams], callable]] = {
    "ema_crossover": (ema_crossover.Params, ema_crossover.generate_signal),
    "rsi_mean_reversion": (rsi_mean_reversion.Params, rsi_mean_reversion.generate_signal),
    "bollinger_reversion": (bollinger.Params, bollinger.generate_signal),
    "atr_trend": (atr_trend.Params, atr_trend.generate_signal),
    "volume_breakout": (volume_breakout.Params, volume_breakout.generate_signal),
}


def validate_params(strategy_type: str, raw_params: dict) -> StrategyParams:
    if strategy_type not in STRATEGY_REGISTRY:
        raise ValueError(f"unknown strategy_type {strategy_type!r}, must be one of {sorted(STRATEGY_REGISTRY)}")
    params_class, _ = STRATEGY_REGISTRY[strategy_type]
    return params_class(**raw_params)


def generate_signal(strategy_type: str, candles: list[dict], raw_params: dict, has_position: bool) -> StrategySignal:
    params = validate_params(strategy_type, raw_params)
    _, fn = STRATEGY_REGISTRY[strategy_type]
    return fn(candles, params, has_position)
