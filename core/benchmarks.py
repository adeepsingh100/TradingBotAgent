"""Buy-and-hold BTC and hold-cash benchmarks, per wallet, from the same
start date and capital as that wallet (spec section 7).

The BTC benchmark's holding quantity is computed once at wallet
creation and needs to persist between ticks, but doesn't deserve its
own schema/migration -- it's one small JSON blob, so it reuses the
`settings` key-value table under a per-wallet key, the same way every
other small piece of app state that isn't core trading data does.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.db.models import EquityHistory, Setting, Wallet
from core.fees import simulate_fill


def _benchmark_key(wallet_id) -> str:
    return f"benchmark_btc:{wallet_id}"


def initialize_benchmarks(session, wallet: Wallet, btc_price: float, costs_settings: dict) -> None:
    """Call once, when a wallet is created (or reset) -- buys BTC with
    the wallet's full starting capital (entry costs included, per spec
    section 7's "costs included at entry"), records both benchmarks'
    first equity_history point."""
    fill = simulate_fill(
        "buy", qty=1.0, price=btc_price, fee_pct=costs_settings["taker_fee_pct"], slippage_pct=costs_settings["slippage_pct"],
        gst_pct=costs_settings.get("gst_pct", 0.0),
    )
    # fill.cash_delta is the signed cost of buying 1 unit at this price+fee -- scale to spend exactly starting_capital
    btc_qty = wallet.starting_capital / abs(fill.cash_delta)

    session.add(
        Setting(
            key=_benchmark_key(wallet.id),
            value={"btc_qty": btc_qty, "entry_price": btc_price, "entry_at": datetime.now(timezone.utc).isoformat()},
        )
    )

    now = datetime.now(timezone.utc)
    session.add(EquityHistory(wallet_id=wallet.id, series="benchmark_btc", equity_inr=wallet.starting_capital,
                               holdings_value_inr=wallet.starting_capital, recorded_at=now))
    session.add(EquityHistory(wallet_id=wallet.id, series="benchmark_cash", equity_inr=wallet.starting_capital,
                               cash_inr=wallet.starting_capital, recorded_at=now))


def record_benchmark_tick(session, wallet: Wallet, btc_price: float, rent_paid_inr: float = 0.0) -> None:
    """Call every tick alongside the agent's own equity_history write.
    benchmark_cash is a flat line at starting_capital by definition --
    still written every tick so it shares the same timestamps as the
    other two curves for charting (spec section 7: "show all equity
    curves together"). Both benchmarks pay the same daily running cost
    as the agent (core/running_cost.py), so the chart answers "does
    trading beat sitting still and paying rent", not a rent-free strawman."""
    state = session.query(Setting).filter_by(key=_benchmark_key(wallet.id)).one()
    btc_qty = state.value["btc_qty"]
    now = datetime.now(timezone.utc)

    session.add(EquityHistory(wallet_id=wallet.id, series="benchmark_btc", equity_inr=btc_qty * btc_price - rent_paid_inr,
                               holdings_value_inr=btc_qty * btc_price, recorded_at=now))
    session.add(EquityHistory(wallet_id=wallet.id, series="benchmark_cash", equity_inr=wallet.starting_capital - rent_paid_inr,
                               cash_inr=wallet.starting_capital, recorded_at=now))
