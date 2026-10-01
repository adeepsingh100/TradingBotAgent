"""Cost model for a simulated fill -- pure functions, no DB/network, so
every number here is independently testable. core/paper_engine.py is
the only caller; it owns persisting the result.

TDS semantics (spec section 6, and section 8's equity formula):
TDS is withheld from sale proceeds -- it reduces the cash a sell
actually credits -- but it is tracked separately as a tax credit
(`Wallet.tds_credit`), NOT folded into the equity number the death
threshold is checked against. Spec section 8, read literally: "Equity
= INR cash + market value of holdings (TDS credits shown separately)."
TDS credit is a separate display line, not part of survival equity.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FillResult:
    fill_price: float
    notional: float
    fee: float
    tds: float
    slippage_amount: float
    cash_delta: float  # negative (cash spent) on buy, positive (cash credited) on sell, net of fee/tds


def apply_slippage(price: float, side: str, slippage_pct: float) -> float:
    """A buy fills worse (higher) than quoted; a sell fills worse
    (lower) -- slippage always works against the trader, simulating a
    real fill rather than an idealized one."""
    factor = slippage_pct / 100
    if side == "buy":
        return price * (1 + factor)
    if side == "sell":
        return price * (1 - factor)
    raise ValueError(f"side must be 'buy' or 'sell', got {side!r}")


def simulate_fill(side: str, qty: float, price: float, *, fee_pct: float, slippage_pct: float, tds_pct: float = 0.0) -> FillResult:
    """`fee_pct` should be the taker rate -- the paper engine always
    assumes an immediate market-style fill against the live price, not
    a resting maker order, so taker is the conservative (and simpler)
    choice; see core/paper_engine.py's module docstring."""
    fill_price = apply_slippage(price, side, slippage_pct)
    notional = qty * fill_price
    fee = notional * fee_pct / 100
    slippage_amount = abs(fill_price - price) * qty

    if side == "buy":
        return FillResult(fill_price, notional, fee, 0.0, slippage_amount, cash_delta=-(notional + fee))

    tds = notional * tds_pct / 100
    return FillResult(fill_price, notional, fee, tds, slippage_amount, cash_delta=notional - fee - tds)


def roundtrip_cost_pct(*, taker_fee_pct: float, tds_pct: float, slippage_pct: float) -> float:
    """Entry fee + exit fee + exit TDS + entry slippage + exit slippage,
    as a % of notional -- what risk_manager compares a trade's expected
    reward against (spec section 9's 1.5x rule)."""
    return 2 * taker_fee_pct + tds_pct + 2 * slippage_pct
