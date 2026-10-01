"""Deterministic, no LLM, no DB -- every rule from spec section 9 as a
pure function over a `Proposal` + `RiskContext` snapshot the caller
(worker, Phase 5) assembles from the DB. Spot-only/no-leverage isn't a
rule checked here -- it's structural: `Proposal.action` only ever means
open-a-long (`buy`) or close-a-long (`sell`), there's no short/margin
concept anywhere in this module to disable.

Exits are never gated the same way entries are -- closing risk is
itself a capital-preservation action, so a `sell` proposal is always
approved (unless it's missing the position it claims to close; the
caller catches that, not this module). Only a `buy` (opening new
exposure) goes through the full gate stack: kill switch, cooldown,
daily loss limit, position/trade-count caps, confidence floor,
mandatory stop-loss, the 1.5x reward-to-cost check, then sizing.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from pydantic import BaseModel


class Proposal(BaseModel):
    """Shape the LLM's decide node (Phase 5) returns via structured
    output -- defined here, not in worker/, since this module is what
    validates it and paper_engine is what executes an approved one."""

    action: str  # buy | sell | hold
    pair: str
    size_inr: float | None = None
    entry: float | None = None
    stop_loss: float | None = None
    take_profit: float | None = None
    strategy_id: str | None = None
    confidence: float = 0.0
    reasoning: str = ""


@dataclass(frozen=True)
class RiskContext:
    equity: float
    open_positions_count: int
    trades_today_count: int
    daily_pnl_pct: float  # negative on a losing day
    cooldown_until: datetime | None
    now: datetime
    kill_switch: bool
    risk_settings: dict  # the `settings` table's "risk" key
    costs_settings: dict  # the `settings` table's "costs" key


@dataclass(frozen=True)
class RiskVerdict:
    verdict: str  # approved | downsized | rejected | hold
    reason: str
    approved_qty: float | None = None
    approved_size_inr: float | None = None


def consecutive_losses(recent_trade_pnls_most_recent_first: list[float]) -> int:
    """How many losing trades in a row, counting back from the most
    recent -- stops at the first winner. Caller (worker) feeds this the
    wallet's closed trades ordered newest-first; used to decide whether
    the cooldown (spec section 9) should trigger."""
    count = 0
    for pnl in recent_trade_pnls_most_recent_first:
        if pnl is not None and pnl < 0:
            count += 1
        else:
            break
    return count


def evaluate(proposal: Proposal, ctx: RiskContext) -> RiskVerdict:
    if proposal.action == "hold":
        return RiskVerdict("hold", "agent proposed hold")

    if proposal.action == "sell":
        return RiskVerdict("approved", "exit -- closing risk is never blocked")

    if proposal.action != "buy":
        return RiskVerdict("rejected", f"unknown action {proposal.action!r}")

    risk = ctx.risk_settings
    costs = ctx.costs_settings

    if ctx.kill_switch:
        return RiskVerdict("rejected", "kill switch active")
    if ctx.cooldown_until is not None and ctx.now < ctx.cooldown_until:
        return RiskVerdict("rejected", f"cooldown active until {ctx.cooldown_until.isoformat()}")
    if ctx.daily_pnl_pct <= -risk["daily_loss_limit_pct"]:
        return RiskVerdict("rejected", "daily loss limit hit -- paused until next day (IST)")
    if ctx.open_positions_count >= risk["max_open_positions"]:
        return RiskVerdict("rejected", "max open positions reached")
    if ctx.trades_today_count >= risk["max_trades_per_day"]:
        return RiskVerdict("rejected", "max trades per day reached")
    if proposal.confidence < risk["min_confidence"]:
        return RiskVerdict("rejected", f"confidence {proposal.confidence} below minimum {risk['min_confidence']}")
    if proposal.stop_loss is None:
        return RiskVerdict("rejected", "no stop-loss provided -- mandatory on every trade")
    if proposal.entry is None or proposal.take_profit is None:
        return RiskVerdict("rejected", "missing entry or take_profit")

    stop_distance = abs(proposal.entry - proposal.stop_loss)
    if stop_distance <= 0:
        return RiskVerdict("rejected", "stop_loss must differ from entry")

    target_distance = abs(proposal.take_profit - proposal.entry)
    from core.fees import roundtrip_cost_pct  # local import -- avoids a module-load-order cycle with fees.py

    cost_pct = roundtrip_cost_pct(
        taker_fee_pct=costs["taker_fee_pct"], tds_pct=costs["tds_pct"], slippage_pct=costs["slippage_pct"],
        gst_pct=costs.get("gst_pct", 0.0),
    )
    cost_distance = proposal.entry * cost_pct / 100
    min_multiple = risk["min_reward_to_cost_multiple"]
    if target_distance < min_multiple * cost_distance:
        return RiskVerdict(
            "rejected",
            f"take-profit distance ({target_distance:.2f}) doesn't clear round-trip costs "
            f"({cost_distance:.2f}) by the required {min_multiple}x",
        )

    max_size_inr = ctx.equity * risk["max_position_size_pct"] / 100
    risk_budget_inr = ctx.equity * risk["max_risk_per_trade_pct"] / 100
    risk_derived_size_inr = (risk_budget_inr / stop_distance) * proposal.entry

    proposed_size_inr = proposal.size_inr if proposal.size_inr is not None else max_size_inr
    approved_size_inr = min(proposed_size_inr, max_size_inr, risk_derived_size_inr)
    if approved_size_inr <= 0:
        return RiskVerdict("rejected", "approved size rounds to zero -- equity too low or stop too wide")

    approved_qty = approved_size_inr / proposal.entry
    verdict = "approved" if approved_size_inr >= proposed_size_inr - 1e-9 else "downsized"
    reason = "within all limits" if verdict == "approved" else (
        f"downsized from {proposed_size_inr:.2f} to {approved_size_inr:.2f} INR "
        f"(position-size/risk-per-trade cap)"
    )
    return RiskVerdict(verdict, reason, approved_qty=approved_qty, approved_size_inr=approved_size_inr)
