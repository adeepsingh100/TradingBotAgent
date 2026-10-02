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


class ParamValue(BaseModel):
    name: str
    value: float


class ParamCandidate(BaseModel):
    params: list[ParamValue]
    reasoning: str = ""


class ResearchOutput(BaseModel):
    """Structured-output shape for the strategy-research LLM call
    (worker/research.py). Params are a list of name/value pairs, not a
    free-form dict -- strict JSON-schema mode needs fixed properties."""

    candidates: list[ParamCandidate] = []
