"""Daily running cost ("rent") -- every alive wallet pays a fixed amount
per day just to keep running, so doing nothing slowly kills it and the
agent must out-earn the rent to survive.

A separate ledger subtracted from equity, never deducted from
`wallet.current_cash`: a live wallet's cash mirrors the real CoinDCX
balance, and a virtual deduction there would desync it. Paper uses the
same ledger so paper and live stay comparable. `effective_equity` is
what every decision sees (risk sizing, the survival brief, the death
check, the equity chart).

Per-wallet state reuses the `settings` key-value table, same as
core/benchmarks.py's `benchmark_btc:{wallet_id}`. A wallet reset drops
the key (worker/cycle.py::_apply_pending_resets), so rent starts fresh.
"""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

from core.db.models import Setting

IST = ZoneInfo("Asia/Kolkata")
DEFAULT_RUNNING_COST = {"paper_daily_inr": 50.0, "live_daily_inr": 5.0}


def _key(wallet_id) -> str:
    return f"running_cost:{wallet_id}"


def today_ist() -> date:
    return datetime.now(IST).date()


def daily_cost(cfg: dict, wallet_kind: str) -> float:
    cfg = {**DEFAULT_RUNNING_COST, **(cfg or {})}
    return float(cfg["live_daily_inr" if wallet_kind == "live" else "paper_daily_inr"])


def accrue_rent(session, wallet, daily_inr: float, today: date) -> float:
    """Charges any days not yet paid, including today on first sight and
    every day missed while the worker slept. Returns the total paid so
    far. Call only for alive wallets -- a paused/dead wallet pays nothing."""
    row = session.query(Setting).filter_by(key=_key(wallet.id)).one_or_none()
    if row is None:
        total = daily_inr  # day 1's rent, paid on first sight
        session.add(Setting(key=_key(wallet.id), value={"charged_through": today.isoformat(), "total_inr": total}))
        return total
    charged_through = date.fromisoformat(row.value["charged_through"])
    days = (today - charged_through).days
    total = float(row.value["total_inr"])
    if days > 0:
        total += days * daily_inr
        row.value = {"charged_through": today.isoformat(), "total_inr": total}
    return total


def rent_paid(session, wallet_id) -> float:
    row = session.query(Setting).filter_by(key=_key(wallet_id)).one_or_none()
    return float(row.value["total_inr"]) if row is not None else 0.0


def runway_days(effective_equity: float, death_line: float, daily_inr: float) -> int:
    if daily_inr <= 0:
        return 0
    return max(0, int((effective_equity - death_line) // daily_inr))
