"""Structured-output shape for the `strategize` LLM node. The `decide`
node reuses `core.risk_manager.Proposal` directly -- it's already the
exact shape risk_manager/paper_engine consume, no reason for a second
schema that means the same thing.
"""

from __future__ import annotations

from pydantic import BaseModel


class StrategyAssignment(BaseModel):
    pair: str
    strategy_type: str
    reasoning: str = ""


class StrategizeOutput(BaseModel):
    assignments: list[StrategyAssignment] = []
