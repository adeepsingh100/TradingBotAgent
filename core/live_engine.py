"""Real-order equivalent of core/paper_engine.py -- the exchange is the
source of truth for fill price/qty/fee here, never core/fees.py's
simulate_fill() estimate. Every entrypoint:

1. Never lets an exception escape -- same "any error -> safe default,
   never crash the tick" discipline core/llm/provider.py::call_structured
   established for LLM calls, extended here to order placement.
2. Commits each durability checkpoint on the SAME session the caller
   passed in, rather than waiting for worker/cycle.py's outer
   transaction -- a REAL order, once filled, is an external fact a
   later unrelated exception in the same tick must never be able to
   erase from the DB. paper_engine correctly relies on that outer
   transaction since nothing it does is external/irreversible; this
   module deliberately diverges, on purpose, every time.
3. Writes a `LiveOrder` audit row (status="pending_submit") BEFORE
   ever calling the exchange -- the ground truth that makes a crash
   mid-placement reconstructable from the DB + the exchange's own
   order history (client.get_order_status/get_order_trade_history),
   not something this module has to get exactly right the first time.

Market orders only, both entries and exits, never limit -- keeps live
fills comparable to the taker-fill assumption paper's PnL (and
therefore the promotion track record that justified going live) was
built on. No on-exchange stop-loss exists for CoinDCX spot (Phase 2
finding, README's Known risks) -- stops are simulated by polling, same
as paper mode, except triggering one here submits a real market sell.

**Order lifecycle fields/status values below are from docs.coindcx.com,
UNVERIFIED against a real order** -- the account had ~₹1 INR balance
(dust) when this was written, not enough to clear any pair's minimum
notional. Verify against a real tiny order before trusting this module
with meaningful size; see README's Known risks.
"""

from __future__ import annotations

import time
import uuid as uuid_module
from datetime import datetime, timezone

from core.coindcx import client
from core.coindcx.market_cache import meets_min_notional, round_quantity
from core.db.models import LiveOrder, Position, Trade, Wallet

# init/open/partially_filled = still open; filled/partially_cancelled/
# cancelled/rejected = settled. Per docs.coindcx.com -- see module docstring.
_OPEN_STATUSES = {"init", "open", "partially_filled"}

POLL_INTERVAL_SECONDS = 2
POLL_BUDGET_SECONDS = 20


def _client_order_id() -> str:
    return f"survivor-{uuid_module.uuid4().hex[:24]}"


def _poll_until_settled(order_id: str, *, budget_seconds: float = None, interval_seconds: float = None) -> dict | None:
    """Never raises -- a network hiccup mid-poll just ends the loop
    early; the caller treats "still open (or unreadable) after budget"
    as a timeout, not a crash.

    `budget_seconds`/`interval_seconds` read the current module-level
    POLL_BUDGET_SECONDS/POLL_INTERVAL_SECONDS when left as None (the
    default), not a value frozen at import time -- tests monkeypatch
    those module constants down to near-zero, which only works if the
    lookup happens at call time."""
    if budget_seconds is None:
        budget_seconds = POLL_BUDGET_SECONDS
    if interval_seconds is None:
        interval_seconds = POLL_INTERVAL_SECONDS
    deadline = time.monotonic() + budget_seconds
    last = None
    while time.monotonic() < deadline:
        try:
            last = client.get_order_status(order_id)
        except Exception:  # noqa: BLE001 -- keep polling on the existing `last`, never crash the tick over a flaky read
            break
        if last.get("status") not in _OPEN_STATUSES:
            return last
        time.sleep(interval_seconds)
    return last


def _find_by_client_order_id(market: str, client_order_id: str) -> dict | None:
    """Used only when create_order itself throws before an order id
    comes back -- disambiguates whether the order actually reached the
    exchange despite the client-side error."""
    try:
        result = client.get_active_orders(market=market)
    except Exception:  # noqa: BLE001
        return None
    for order in result.get("orders", []):
        if order.get("client_order_id") == client_order_id:
            return order
    return None


def _real_fill(order_id: str, fallback: dict, requested_qty: float) -> tuple[float, float, float]:
    """(qty, avg_price, total_fee) from the real trade_history for this
    order -- falls back to the order-status response's own
    avg_price/remaining_quantity if trade_history is empty/unreadable
    (less precise, still real exchange data, never a simulated guess)."""
    try:
        trades = client.get_order_trade_history(order_id)
    except Exception:  # noqa: BLE001
        trades = None

    if trades:
        qty = sum(float(t["quantity"]) for t in trades)
        if qty > 0:
            notional = sum(float(t["quantity"]) * float(t["price"]) for t in trades)
            fee = sum(float(t.get("fee_amount", 0)) for t in trades)
            return qty, notional / qty, fee

    filled_qty = float(fallback.get("total_quantity", requested_qty)) - float(fallback.get("remaining_quantity", 0))
    avg_price = float(fallback.get("avg_price") or 0)
    fee = float(fallback.get("fee_amount", 0))
    return filled_qty, avg_price, fee


def _submit_and_resolve(session, wallet: Wallet, *, pair: str, side: str, qty: float,
                         step: float, decision_id, allow_live: bool) -> dict:
    """Submit + poll + reconcile one market order. Returns a dict with
    at least `status`; a resolved fill also has `qty`/`price`/`fee`.
    Never raises -- every failure mode resolves to a LiveOrder status
    instead."""
    qty = round_quantity(qty, step)
    client_order_id = _client_order_id()

    live_order = LiveOrder(
        wallet_id=wallet.id, decision_id=decision_id, pair=pair, side=side,
        client_order_id=client_order_id, status="pending_submit", requested_qty=qty,
    )
    session.add(live_order)
    session.commit()

    try:
        response = client.create_order(
            market=pair, side=side, order_type="market_order",
            total_quantity=qty, client_order_id=client_order_id, allow_live=allow_live,
        )
    except Exception as exc:  # noqa: BLE001 -- the ambiguous case: did this reach the exchange or not?
        found = _find_by_client_order_id(pair, client_order_id)
        if found is None:
            live_order.status, live_order.error = "error", str(exc)[:2000]
            session.commit()
            return {"status": "error"}
        order_id = found.get("id")
        live_order.exchange_order_id, live_order.status = order_id, "submitted"
        session.commit()
    else:
        if response.get("dry_run"):
            live_order.status, live_order.raw_response = "dry_run", response
            session.commit()
            return {"status": "dry_run"}

        order_id = response.get("id")
        live_order.exchange_order_id, live_order.status, live_order.raw_response = order_id, "submitted", response
        session.commit()

        if not order_id:
            live_order.status = "unknown_needs_manual_check"
            session.commit()
            return {"status": "unknown_needs_manual_check"}

    # Both branches above converge here once a real order_id is known --
    # the create-order-threw-but-found-via-active-orders case needs the
    # exact same poll/cancel/resolve sequence as the happy path, not a
    # shortcut that trusts active_orders' one-off snapshot as final.
    settled = _poll_until_settled(order_id)
    if settled is None or settled.get("status") in _OPEN_STATUSES:
        try:
            client.cancel_order(order_id)
        except Exception:  # noqa: BLE001 -- still worth the final re-poll below even if the cancel call itself failed
            pass
        settled = _poll_until_settled(order_id, budget_seconds=POLL_INTERVAL_SECONDS * 2)
        if settled is None:
            live_order.status = "unknown_needs_manual_check"
            session.commit()
            return {"status": "unknown_needs_manual_check"}
        if settled.get("status") in _OPEN_STATUSES:
            live_order.status = "timed_out_cancelled"
            session.commit()
            return {"status": "timed_out_cancelled"}

    status = settled.get("status")
    fill_qty, avg_price, fee = _real_fill(order_id, settled, qty)

    live_order.filled_qty, live_order.filled_price, live_order.raw_response = fill_qty, avg_price, settled
    live_order.status = "filled" if status == "filled" else ("partially_filled" if fill_qty > 0 else status)
    session.commit()

    if fill_qty <= 0:
        return {"status": live_order.status}
    return {"status": live_order.status, "qty": fill_qty, "price": avg_price, "fee": fee}


def open_position(
    session, wallet: Wallet, *, pair: str, qty: float, price_hint: float, stop_loss: float, take_profit: float,
    costs_settings: dict, market_cache_row, strategy_id=None, decision_id=None, allow_live: bool,
) -> Position | None:
    try:
        qty = round_quantity(qty, market_cache_row.step)
        if qty <= 0 or not meets_min_notional(qty, price_hint, market_cache_row.min_notional):
            return None  # below the exchange's own minimum -- no point even submitting

        result = _submit_and_resolve(
            session, wallet, pair=pair, side="buy", qty=qty, step=market_cache_row.step,
            decision_id=decision_id, allow_live=allow_live,
        )
        if "qty" not in result or result["qty"] <= 0:
            return None

        fill_qty, fill_price, fee = result["qty"], result["price"], result["fee"]
        wallet.current_cash -= fill_qty * fill_price + fee

        position = Position(
            wallet_id=wallet.id, pair=pair, side="buy", qty=fill_qty, entry_price=fill_price,
            stop_loss=stop_loss, take_profit=take_profit, strategy_id=strategy_id, decision_id=decision_id,
        )
        session.add(position)
        session.flush()
        session.add(Trade(
            wallet_id=wallet.id, position_id=position.id, pair=pair, side="buy", qty=fill_qty, price=fill_price,
            fee=fee, tds=0.0, slippage=0.0, pnl=None, strategy_id=strategy_id, decision_id=decision_id,
        ))
        session.commit()
        return position
    except Exception:  # noqa: BLE001 -- a failed entry degrades to "no position opened," never crashes the tick
        return None


def close_position(
    session, wallet: Wallet, position: Position, *, costs_settings: dict, market_cache_row, decision_id=None,
    allow_live: bool,
) -> Trade | None:
    try:
        result = _submit_and_resolve(
            session, wallet, pair=position.pair, side="sell", qty=position.qty, step=market_cache_row.step,
            decision_id=decision_id, allow_live=allow_live,
        )
        if "qty" not in result or result["qty"] <= 0:
            return None

        fill_qty, fill_price, fee = result["qty"], result["price"], result["fee"]
        notional = fill_qty * fill_price
        # Our own tds_pct estimate applied to the real notional -- what CoinDCX
        # actually withholds for TDS isn't visible in trade_history's documented
        # fields (UNVERIFIED, see module docstring); reconcile against the
        # exchange's own statement before trusting this number for tax filing.
        tds = notional * costs_settings.get("tds_pct", 0) / 100
        cash_credit = notional - fee - tds
        wallet.current_cash += cash_credit
        wallet.tds_credit += tds

        entry_trade = session.query(Trade).filter_by(position_id=position.id, side="buy").order_by(Trade.executed_at).first()
        cost_basis_per_unit = (
            (entry_trade.qty * entry_trade.price + entry_trade.fee) / entry_trade.qty if entry_trade else position.entry_price
        )
        pnl = cash_credit - cost_basis_per_unit * fill_qty

        exit_trade = Trade(
            wallet_id=wallet.id, position_id=position.id, pair=position.pair, side="sell", qty=fill_qty,
            price=fill_price, fee=fee, tds=tds, slippage=0.0, pnl=pnl,
            strategy_id=position.strategy_id, decision_id=decision_id,
        )
        session.add(exit_trade)

        if fill_qty >= position.qty - 1e-12:
            position.qty = fill_qty
            position.closed_at = datetime.now(timezone.utc)
        else:
            position.qty -= fill_qty  # partial exit -- stays open, next tick's check_exits retries the remainder

        session.commit()
        return exit_trade
    except Exception:  # noqa: BLE001 -- a failed exit leaves the position open for next tick to retry, never crashes the tick
        return None


def kill_wallet(session, wallet: Wallet, agent, open_positions: list[Position], costs_settings: dict,
                 market_cache_by_pair: dict, allow_live: bool) -> None:
    """Mirrors paper_engine.kill_wallet -- closes every open position
    (real market sells here) then marks the agent permanently dead.
    A position whose pair has no market_cache row is left open and
    skipped (can't round/validate a quantity without it) -- the agent
    still gets marked dead either way; this is a real-money edge case
    worth a human looking at, not one this function can safely
    auto-resolve."""
    for position in open_positions:
        market_cache_row = market_cache_by_pair.get(position.pair)
        if market_cache_row is None:
            continue
        close_position(
            session, wallet, position, costs_settings=costs_settings,
            market_cache_row=market_cache_row, allow_live=allow_live,
        )

    agent.status = "dead"
    agent.died_at = datetime.now(timezone.utc)
    session.commit()
