"""Mocked HTTP only (via core.coindcx.client's requests calls) -- no
real network/DB in the suite, matching repo convention. Poll timing is
collapsed to near-zero via monkeypatched module constants so these
tests run fast; see core/live_engine.py::_poll_until_settled's
docstring for why that's call-time, not def-time, resolution."""

from __future__ import annotations

import uuid

import pytest
import requests

from core import live_engine
from core.db.models import Agent, MarketCache, Position, Trade, Wallet
from tests.conftest import FakeSession

PAIR = "BTCINR"


class _FakeResponse:
    def __init__(self, json_data):
        self._json = json_data

    def json(self):
        return self._json

    def raise_for_status(self):
        pass


def _fake_post_sequence(monkeypatch, responses_by_path: dict[str, list | dict]):
    """responses_by_path maps the URL path suffix (e.g.
    "/exchange/v1/orders/create") to either a single response dict
    (reused every call) or a list consumed in order."""
    calls = []

    def fake_post(url, data=None, headers=None, timeout=10):
        path = url.split(".com", 1)[1]
        calls.append(path)
        entry = responses_by_path[path]
        if isinstance(entry, list):
            # Pop down to the last item, then keep repeating it -- a poll loop
            # with no real sleep (time.sleep is mocked to a no-op below) can
            # call this far more times than a short fixture list anticipates.
            return _FakeResponse(entry.pop(0) if len(entry) > 1 else entry[0])
        return _FakeResponse(entry)

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(live_engine.client.time, "sleep", lambda _: None)  # client.py's own retry backoff
    monkeypatch.setattr(live_engine, "POLL_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(live_engine, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(live_engine.client.settings, "coindcx_api_key", "k")
    monkeypatch.setattr(live_engine.client.settings, "coindcx_api_secret", "s")
    return calls


def _wallet(cash=1000.0):
    return Wallet(id=uuid.uuid4(), name="live", kind="live", starting_capital=cash, current_cash=cash, tds_credit=0.0)


def _market_cache():
    return MarketCache(pair="I-BTC_INR", min_quantity=0.0001, max_quantity=10, step=0.0001,
                        min_notional=100, base_precision=2, target_precision=4, raw={"symbol": "BTCINR"})


COSTS = {"maker_fee_pct": 0.2, "taker_fee_pct": 0.2, "tds_pct": 1.0, "slippage_pct": 0.1}


def test_open_position_is_a_no_op_dry_run_when_allow_live_is_false(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **kw: calls.append(1))  # should never be called
    session, wallet = FakeSession(), _wallet()

    position = live_engine.open_position(
        session, wallet, pair=PAIR, qty=0.001, price_hint=8_000_000, stop_loss=7_900_000, take_profit=8_300_000,
        costs_settings=COSTS, market_cache_row=_market_cache(), allow_live=False,
    )

    assert position is None
    assert calls == []
    live_orders = [o for o in session.added if o.__class__.__name__ == "LiveOrder"]
    assert live_orders[0].status == "dry_run"


def test_open_position_rejects_below_min_notional_without_any_network_call(monkeypatch):
    calls = []
    monkeypatch.setattr(requests, "post", lambda *a, **kw: calls.append(1))
    session, wallet = FakeSession(), _wallet()

    position = live_engine.open_position(
        session, wallet, pair=PAIR, qty=0.00001, price_hint=8_000_000, stop_loss=1, take_profit=1,
        costs_settings=COSTS, market_cache_row=_market_cache(), allow_live=True,
    )

    assert position is None
    assert calls == []  # rejected before ever hitting the network -- real notional is far below 100


def test_open_position_full_fill_writes_position_and_trade_from_real_numbers(monkeypatch):
    responses = {
        "/exchange/v1/orders/create": [{"id": "ord1", "status": "open"}],
        "/exchange/v1/orders/status": [{"id": "ord1", "status": "filled", "total_quantity": 0.001, "remaining_quantity": 0}],
        "/exchange/v1/orders/trade_history": [[{"id": "t1", "order_id": "ord1", "quantity": 0.001, "price": 8_001_000, "fee_amount": 1.5}]],
    }
    _fake_post_sequence(monkeypatch, responses)
    session, wallet = FakeSession(), _wallet(cash=10000.0)

    position = live_engine.open_position(
        session, wallet, pair=PAIR, qty=0.001, price_hint=8_000_000, stop_loss=7_900_000, take_profit=8_300_000,
        costs_settings=COSTS, market_cache_row=_market_cache(), allow_live=True,
    )

    assert position is not None
    assert position.entry_price == 8_001_000
    assert position.qty == pytest.approx(0.001)
    assert wallet.current_cash == pytest.approx(10000.0 - (0.001 * 8_001_000 + 1.5))
    trades = [o for o in session.added if isinstance(o, Trade)]
    assert len(trades) == 1 and trades[0].side == "buy" and trades[0].fee == 1.5
    live_orders = [o for o in session.added if o.__class__.__name__ == "LiveOrder"]
    assert live_orders[0].status == "filled"


def test_open_position_never_fills_gets_cancelled_and_returns_none(monkeypatch):
    responses = {
        "/exchange/v1/orders/create": [{"id": "ord2", "status": "open"}],
        "/exchange/v1/orders/status": [{"id": "ord2", "status": "open"}] * 10,  # stays open the whole poll budget
        "/exchange/v1/orders/cancel": [{"id": "ord2", "status": "cancelled"}],
    }
    _fake_post_sequence(monkeypatch, responses)
    session, wallet = FakeSession(), _wallet()

    position = live_engine.open_position(
        session, wallet, pair=PAIR, qty=0.001, price_hint=8_000_000, stop_loss=1, take_profit=1,
        costs_settings=COSTS, market_cache_row=_market_cache(), allow_live=True,
    )

    assert position is None
    live_orders = [o for o in session.added if o.__class__.__name__ == "LiveOrder"]
    assert live_orders[0].status == "timed_out_cancelled"


def test_open_position_create_order_throws_but_order_found_via_active_orders_resumes(monkeypatch):
    monkeypatch.setattr(live_engine, "_client_order_id", lambda: "fixed-client-order-id")

    def fake_post(url, data=None, headers=None, timeout=10):
        path = url.split(".com", 1)[1]
        if path == "/exchange/v1/orders/create":
            raise requests.ConnectionError("boom")
        if path == "/exchange/v1/orders/active_orders":
            return _FakeResponse({"orders": [{"id": "ord3", "client_order_id": "fixed-client-order-id", "status": "open"}]})
        if path == "/exchange/v1/orders/status":
            return _FakeResponse({"id": "ord3", "status": "filled", "total_quantity": 0.001, "remaining_quantity": 0})
        if path == "/exchange/v1/orders/trade_history":
            return _FakeResponse([{"id": "t1", "order_id": "ord3", "quantity": 0.001, "price": 8_000_000, "fee_amount": 1.0}])
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(live_engine.client.time, "sleep", lambda _: None)
    monkeypatch.setattr(live_engine, "POLL_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(live_engine, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(live_engine.client.settings, "coindcx_api_key", "k")
    monkeypatch.setattr(live_engine.client.settings, "coindcx_api_secret", "s")

    session, wallet = FakeSession(), _wallet(cash=10000.0)
    position = live_engine.open_position(
        session, wallet, pair=PAIR, qty=0.001, price_hint=8_000_000, stop_loss=1, take_profit=1,
        costs_settings=COSTS, market_cache_row=_market_cache(), allow_live=True,
    )

    assert position is not None
    assert position.entry_price == 8_000_000
    live_orders = [o for o in session.added if o.__class__.__name__ == "LiveOrder"]
    assert live_orders[0].status == "filled"


def test_open_position_create_order_throws_and_order_never_found_is_an_error(monkeypatch):
    def fake_post(url, data=None, headers=None, timeout=10):
        path = url.split(".com", 1)[1]
        if path == "/exchange/v1/orders/create":
            raise requests.ConnectionError("boom")
        if path == "/exchange/v1/orders/active_orders":
            return _FakeResponse({"orders": []})
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(live_engine.client.time, "sleep", lambda _: None)
    monkeypatch.setattr(live_engine.client.settings, "coindcx_api_key", "k")
    monkeypatch.setattr(live_engine.client.settings, "coindcx_api_secret", "s")

    session, wallet = FakeSession(), _wallet()
    position = live_engine.open_position(
        session, wallet, pair=PAIR, qty=0.001, price_hint=8_000_000, stop_loss=1, take_profit=1,
        costs_settings=COSTS, market_cache_row=_market_cache(), allow_live=True,
    )

    assert position is None
    live_orders = [o for o in session.added if o.__class__.__name__ == "LiveOrder"]
    assert live_orders[0].status == "error"


def test_close_position_full_fill_computes_pnl_from_real_fill_and_marks_closed(monkeypatch):
    responses = {
        "/exchange/v1/orders/create": [{"id": "ord4", "status": "open"}],
        "/exchange/v1/orders/status": [{"id": "ord4", "status": "filled", "total_quantity": 0.001, "remaining_quantity": 0}],
        "/exchange/v1/orders/trade_history": [[{"id": "t2", "order_id": "ord4", "quantity": 0.001, "price": 8_300_000, "fee_amount": 1.6}]],
    }
    _fake_post_sequence(monkeypatch, responses)
    session, wallet = FakeSession(), _wallet(cash=9000.0)
    position = Position(id=uuid.uuid4(), wallet_id=wallet.id, pair=PAIR, side="buy", qty=0.001,
                         entry_price=8_000_000, stop_loss=7_900_000, take_profit=8_300_000)
    session.added.append(position)
    session.added.append(Trade(wallet_id=wallet.id, position_id=position.id, pair=PAIR, side="buy",
                                qty=0.001, price=8_000_000, fee=1.6))

    trade = live_engine.close_position(
        session, wallet, position, costs_settings=COSTS, market_cache_row=_market_cache(), allow_live=True,
    )

    assert trade is not None
    assert trade.side == "sell"
    assert position.closed_at is not None
    assert trade.tds > 0
    assert wallet.tds_credit == pytest.approx(trade.tds)


def test_close_position_partial_fill_reduces_qty_and_leaves_position_open(monkeypatch):
    # Realistic exchange behavior: status reads "partially_filled" (an OPEN-
    # equivalent status per docs.coindcx.com) until cancel_order is actually
    # called, then flips to the settled "partially_cancelled" on the next read.
    cancelled = {"done": False}

    def fake_post(url, data=None, headers=None, timeout=10):
        path = url.split(".com", 1)[1]
        if path == "/exchange/v1/orders/create":
            return _FakeResponse({"id": "ord5", "status": "open"})
        if path == "/exchange/v1/orders/cancel":
            cancelled["done"] = True
            return _FakeResponse({"id": "ord5", "status": "partially_cancelled"})
        if path == "/exchange/v1/orders/status":
            status = "partially_cancelled" if cancelled["done"] else "partially_filled"
            return _FakeResponse({"id": "ord5", "status": status, "total_quantity": 0.001, "remaining_quantity": 0.0004})
        if path == "/exchange/v1/orders/trade_history":
            return _FakeResponse([{"id": "t3", "order_id": "ord5", "quantity": 0.0006, "price": 8_300_000, "fee_amount": 1.0}])
        raise AssertionError(f"unexpected path {path}")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(live_engine.client.time, "sleep", lambda _: None)
    monkeypatch.setattr(live_engine, "POLL_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(live_engine, "POLL_INTERVAL_SECONDS", 0.01)
    monkeypatch.setattr(live_engine.client.settings, "coindcx_api_key", "k")
    monkeypatch.setattr(live_engine.client.settings, "coindcx_api_secret", "s")

    session, wallet = FakeSession(), _wallet(cash=9000.0)
    position = Position(id=uuid.uuid4(), wallet_id=wallet.id, pair=PAIR, side="buy", qty=0.001,
                         entry_price=8_000_000, stop_loss=7_900_000, take_profit=8_300_000)
    session.added.append(position)
    session.added.append(Trade(wallet_id=wallet.id, position_id=position.id, pair=PAIR, side="buy",
                                qty=0.001, price=8_000_000, fee=1.6))

    trade = live_engine.close_position(
        session, wallet, position, costs_settings=COSTS, market_cache_row=_market_cache(), allow_live=True,
    )

    assert trade is not None
    assert trade.qty == pytest.approx(0.0006)
    assert position.closed_at is None  # still open -- only partially exited
    assert position.qty == pytest.approx(0.0004)


def test_kill_wallet_closes_every_open_position_and_marks_agent_dead(monkeypatch):
    responses = {
        "/exchange/v1/orders/create": [{"id": "ord6", "status": "open"}],
        "/exchange/v1/orders/status": [{"id": "ord6", "status": "filled", "total_quantity": 0.001, "remaining_quantity": 0}],
        "/exchange/v1/orders/trade_history": [[{"id": "t4", "order_id": "ord6", "quantity": 0.001, "price": 7_000_000, "fee_amount": 1.0}]],
    }
    _fake_post_sequence(monkeypatch, responses)
    session, wallet = FakeSession(), _wallet(cash=1000.0)
    position = Position(id=uuid.uuid4(), wallet_id=wallet.id, pair=PAIR, side="buy", qty=0.001,
                         entry_price=8_000_000, stop_loss=7_900_000, take_profit=8_300_000)
    session.added.append(position)
    session.added.append(Trade(wallet_id=wallet.id, position_id=position.id, pair=PAIR, side="buy",
                                qty=0.001, price=8_000_000, fee=1.6))
    agent = Agent(id=uuid.uuid4(), wallet_id=wallet.id, status="alive", mode="live")

    live_engine.kill_wallet(session, wallet, agent, [position], COSTS, {PAIR: _market_cache()}, allow_live=True)

    assert agent.status == "dead"
    assert agent.died_at is not None
    assert position.closed_at is not None
