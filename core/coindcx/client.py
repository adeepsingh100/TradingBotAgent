"""CoinDCX REST client -- public market data (no auth) + private
account/order endpoints (HMAC-SHA256 signed).

Every fact below was verified live against the real API while building
this (both the public markets_details response and a real signed
balances call), not taken from docs alone -- docs.coindcx.com's example
response for markets_details shows `order_types` including
`stop_limit`/`take_profit_limit`, but a live query against ALL 339 real
INR spot pairs (2026-10-01) found **zero** that actually offer anything
beyond `limit_order`/`market_order`. There is currently no on-exchange
stop-loss for spot trading on this exchange, in practice, regardless of
what the API schema allows in principle -- see README's "Known risks".
A live engine (Phase 7+) must simulate stops by polling, exactly the
risk spec section 10 asked to flag if this turned out to be the case.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

import requests

from core.config import settings

API_BASE = "https://api.coindcx.com"
PUBLIC_BASE = "https://public.coindcx.com"

# The default for any caller that doesn't pass create_order's
# `allow_live` kwarg explicitly (e.g. a future scratchpad script) --
# NOT the active live-trading gate as of Phase 7. That gate is
# dashboard-controlled by design (user's explicit request): the
# `settings` table's `live_trading.enabled` key, read fresh every
# tick by worker/cycle.py and threaded into create_order's
# `allow_live` param. Keeping this constant False is still real
# defense-in-depth -- it's what create_order() falls back to if any
# future caller forgets the kwarg.
ALLOW_LIVE_ORDERS = False


class CoinDCXError(Exception):
    pass


def _retry(fn, *, max_attempts: int = 4, base_delay: float = 0.5):
    """Exponential backoff on timeouts/connection errors and on 429/5xx
    responses. Anything else (4xx client error, bad signature, etc.)
    raises immediately -- retrying a request that's wrong by
    construction just burns through the rate limit faster."""
    for attempt in range(max_attempts):
        try:
            return fn()
        except (requests.ConnectionError, requests.Timeout) as exc:
            if attempt == max_attempts - 1:
                raise CoinDCXError(f"network error after {max_attempts} attempts: {exc}") from exc
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else None
            if status not in (429, 500, 502, 503, 504) or attempt == max_attempts - 1:
                body = exc.response.text if exc.response is not None else str(exc)
                raise CoinDCXError(f"HTTP {status}: {body}") from exc
        time.sleep(base_delay * (2**attempt))
    raise CoinDCXError("unreachable")  # pragma: no cover


def _get(url: str, params: dict | None = None, timeout: float = 10) -> Any:
    def call():
        resp = requests.get(url, params=params, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    return _retry(call)


def _post_signed(path: str, body: dict, timeout: float = 10) -> Any:
    """CoinDCX's scheme (verified live, not just from docs): JSON-encode
    `body` (with `timestamp` added, ms epoch) using plain `json.dumps`
    defaults -- NOT a compact/sorted encoding, the exact bytes sent must
    be hashed -- then hex HMAC-SHA256 that string with the API secret.
    Key -> X-AUTH-APIKEY header, signature -> X-AUTH-SIGNATURE."""
    body = {**body, "timestamp": int(time.time() * 1000)}
    payload = json.dumps(body)
    signature = hmac.new(settings.coindcx_api_secret.encode(), payload.encode(), hashlib.sha256).hexdigest()
    headers = {
        "Content-Type": "application/json",
        "X-AUTH-APIKEY": settings.coindcx_api_key,
        "X-AUTH-SIGNATURE": signature,
    }

    def call():
        resp = requests.post(f"{API_BASE}{path}", data=payload, headers=headers, timeout=timeout)
        resp.raise_for_status()
        return resp.json()

    return _retry(call)


# --- Public endpoints ---


def get_ticker() -> list[dict]:
    return _get(f"{API_BASE}/exchange/ticker")


def get_markets_details() -> list[dict]:
    """Full per-pair spec: min/max_quantity, min_notional, step,
    base/target_currency_precision, order_types, status. Refreshed
    into the `market_cache` table daily by market_cache.py -- callers
    needing precision/step for an order should read that cache, not
    call this directly on the hot path."""
    return _get(f"{API_BASE}/exchange/v1/markets_details")


def get_candles(pair: str, interval: str, limit: int = 500) -> list[dict]:
    """`pair` is the `pair` field from markets_details (e.g.
    "I-BTC_INR"), NOT `symbol` ("BTCINR") -- the two are different
    identifiers for the same market, confirmed live."""
    return _get(f"{PUBLIC_BASE}/market_data/candles", params={"pair": pair, "interval": interval, "limit": limit})


def get_orderbook(pair: str) -> dict:
    return _get(f"{PUBLIC_BASE}/market_data/orderbook", params={"pair": pair})


def get_public_trade_history(pair: str, limit: int = 30) -> list[dict]:
    return _get(f"{PUBLIC_BASE}/market_data/trade_history", params={"pair": pair, "limit": limit})


# --- Private endpoints (signed) ---


def get_balances() -> list[dict]:
    return _post_signed("/exchange/v1/users/balances", {})


def get_active_orders(market: str | None = None) -> list[dict]:
    body = {"market": market} if market else {}
    return _post_signed("/exchange/v1/orders/active_orders", body)


def get_order_status(order_id: str) -> dict:
    return _post_signed("/exchange/v1/orders/status", {"id": order_id})


def get_order_trade_history(order_id: str) -> list[dict]:
    return _post_signed("/exchange/v1/orders/trade_history", {"id": order_id})


def cancel_order(order_id: str) -> dict:
    """Not gated by ALLOW_LIVE_ORDERS -- cancelling can never create new
    exchange exposure, and the live engine (Phase 7+) needs to be able
    to cancel a stray order even while entry/creation stays locked."""
    return _post_signed("/exchange/v1/orders/cancel", {"id": order_id})


def create_order(
    market: str,
    side: str,
    order_type: str,
    total_quantity: float,
    price_per_unit: float | None = None,
    client_order_id: str | None = None,
    *,
    allow_live: bool = ALLOW_LIVE_ORDERS,
) -> dict:
    """`market` is the `symbol` field from markets_details (e.g.
    "BTCINR"), confirmed from the API's own create-order example --
    different from get_candles' `pair` identifier, don't mix them up.

    Returns a preview dict instead of calling the API unless the
    caller explicitly passes `allow_live=True`. `ALLOW_LIVE_ORDERS`
    (the module constant) is only the default for a caller that
    doesn't pass `allow_live` at all -- a safety fuse for any future
    scratchpad script, not the active gate. The real gate (Phase 7:
    `core/live_engine.py`) lives in the `settings` table's
    `live_trading.enabled` key, read fresh every tick by
    `worker/cycle.py` and passed in explicitly here -- deliberately
    NOT read from the DB in this module, so `client.py` stays
    network-only with zero DB/session dependency and every existing
    test (mocking `requests` alone) keeps working unmodified."""
    body: dict = {"market": market, "side": side, "order_type": order_type, "total_quantity": total_quantity}
    if price_per_unit is not None:
        body["price_per_unit"] = price_per_unit
    if client_order_id is not None:
        body["client_order_id"] = client_order_id

    if not allow_live:
        return {"dry_run": True, "would_submit": body}

    return _post_signed("/exchange/v1/orders/create", body)
