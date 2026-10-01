"""Refreshes `market_cache` from CoinDCX's markets_details (verified
live field names, 2026-10-01) and provides the rounding helpers every
order must go through before submission.

Field names, confirmed against a real response (not just docs): `pair`
(e.g. "I-BTC_INR") is what candles/orderbook/trade_history take;
`symbol` (e.g. "BTCINR") is the create-order `market` field -- these
are two different identifiers for the same market, easy to mix up.
`base_currency_precision` is the price's decimal precision (base
currency here is always INR); `target_currency_precision`/`step` are
the traded asset's quantity precision/increment.
"""

from __future__ import annotations

import math

from core.coindcx import client
from core.db.models import MarketCache
from core.db.session import get_session


def refresh_market_cache(only_inr_spot: bool = True) -> int:
    """Upserts every (or every INR spot) market's details, keyed by
    `pair`. Call once daily (spec section 10) -- there's no webhook/
    push for this, markets_details is a full-catalog pull every time."""
    details = client.get_markets_details()
    if only_inr_spot:
        details = [d for d in details if d.get("ecode") == "I" and d.get("base_currency_short_name") == "INR"]

    count = 0
    with get_session() as session:
        for d in details:
            fields = dict(
                min_quantity=d["min_quantity"],
                max_quantity=d["max_quantity"],
                step=d["step"],
                min_notional=d["min_notional"],
                base_precision=d["base_currency_precision"],
                target_precision=d["target_currency_precision"],
                order_types=d["order_types"],
                raw=d,
            )
            existing = session.query(MarketCache).filter_by(pair=d["pair"]).one_or_none()
            if existing is None:
                session.add(MarketCache(pair=d["pair"], **fields))
            else:
                for key, value in fields.items():
                    setattr(existing, key, value)
            count += 1
    return count


def round_quantity(qty: float, step: float) -> float:
    """Rounds DOWN to the pair's quantity increment -- CoinDCX rejects
    an order whose quantity isn't a multiple of `step`. Rounding down
    (never up) means a sized order never grows past what the risk
    manager approved."""
    if step <= 0:
        return qty
    return round(math.floor(qty / step) * step, 10)


def round_price(price: float, base_precision: int) -> float:
    return round(price, base_precision)


def meets_min_notional(qty: float, price: float, min_notional: float) -> bool:
    return qty * price >= min_notional
