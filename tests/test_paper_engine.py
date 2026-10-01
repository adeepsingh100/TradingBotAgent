"""No real DB -- a tiny in-memory fake session stands in for
SQLAlchemy's, just enough to support paper_engine's actual usage
(add/flush/delete, and one filter_by().order_by().first() query)."""

from __future__ import annotations

import uuid
from datetime import datetime, timezone

import pytest

from core.db.models import Position, Trade, Wallet
from core.paper_engine import check_stop_or_target, close_position, equity, get_open_positions, is_dead, open_position

COSTS = {"maker_fee_pct": 0.2, "taker_fee_pct": 0.2, "tds_pct": 1.0, "slippage_pct": 0.1}


class _FakeQuery:
    def __init__(self, items):
        self._items = list(items)

    def filter_by(self, **kw):
        self._items = [o for o in self._items if all(getattr(o, k) == v for k, v in kw.items())]
        return self

    def order_by(self, *a):
        return self

    def first(self):
        return self._items[0] if self._items else None

    def all(self):
        return list(self._items)


class FakeSession:
    def __init__(self):
        self.added = []
        self.deleted = []

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid.uuid4()
        if hasattr(obj, "executed_at") and obj.executed_at is None:
            obj.executed_at = datetime.now(timezone.utc)
        self.added.append(obj)

    def flush(self):
        pass

    def delete(self, obj):
        self.deleted.append(obj)
        if obj in self.added:
            self.added.remove(obj)

    def query(self, model):
        return _FakeQuery(o for o in self.added if isinstance(o, model))


def _wallet(cash=1000.0, starting=1000.0) -> Wallet:
    return Wallet(id=uuid.uuid4(), name="test", kind="paper", starting_capital=starting, current_cash=cash, tds_credit=0.0)


def test_open_position_deducts_cash_including_fee_and_slippage():
    session = FakeSession()
    wallet = _wallet(cash=1000.0)

    position = open_position(session, wallet, pair="BTCINR", qty=0.0001, entry_price=8000000,
                              stop_loss=7900000, take_profit=8300000, costs_settings=COSTS)

    assert position.entry_price == pytest.approx(8000000 * 1.001)  # 0.1% slippage
    cost = position.qty * position.entry_price * 1.002  # + 0.2% fee
    assert wallet.current_cash == pytest.approx(1000.0 - cost)
    trades = [o for o in session.added if isinstance(o, Trade)]
    assert len(trades) == 1 and trades[0].side == "buy"


def test_open_position_rejects_when_cash_insufficient():
    session = FakeSession()
    wallet = _wallet(cash=1.0)
    with pytest.raises(ValueError):
        open_position(session, wallet, pair="BTCINR", qty=1, entry_price=8000000,
                       stop_loss=7900000, take_profit=8300000, costs_settings=COSTS)


def test_close_position_computes_pnl_net_of_entry_and_exit_costs():
    session = FakeSession()
    wallet = _wallet(cash=1000.0)
    position = open_position(session, wallet, pair="BTCINR", qty=0.0001, entry_price=8000000,
                              stop_loss=7900000, take_profit=8300000, costs_settings=COSTS)
    cash_after_entry = wallet.current_cash

    exit_trade = close_position(session, wallet, position, exit_price=8300000, costs_settings=COSTS)

    assert exit_trade.side == "sell"
    assert exit_trade.tds > 0
    assert wallet.tds_credit == pytest.approx(exit_trade.tds)
    assert wallet.current_cash == pytest.approx(cash_after_entry + exit_trade.pnl + _cost_basis(session))
    assert position.closed_at is not None  # not deleted (see models.py's Position docstring) -- marked closed


def _cost_basis(session) -> float:
    entry = next(o for o in session.added if isinstance(o, Trade) and o.side == "buy")
    return entry.qty * entry.price + entry.fee


def test_close_position_on_a_loss_is_negative_pnl():
    session = FakeSession()
    wallet = _wallet(cash=1000.0)
    position = open_position(session, wallet, pair="BTCINR", qty=0.0001, entry_price=8000000,
                              stop_loss=7000000, take_profit=9000000, costs_settings=COSTS)

    exit_trade = close_position(session, wallet, position, exit_price=7000000, costs_settings=COSTS)

    assert exit_trade.pnl < 0


def test_get_open_positions_excludes_closed_ones():
    session = FakeSession()
    wallet = _wallet(cash=2000.0)
    still_open = open_position(session, wallet, pair="ETHINR", qty=0.001, entry_price=300000,
                                stop_loss=290000, take_profit=320000, costs_settings=COSTS)
    now_closed = open_position(session, wallet, pair="BTCINR", qty=0.0001, entry_price=8000000,
                                stop_loss=7900000, take_profit=8300000, costs_settings=COSTS)
    close_position(session, wallet, now_closed, exit_price=8300000, costs_settings=COSTS)

    open_positions = get_open_positions(session, wallet.id)

    assert open_positions == [still_open]


def test_check_stop_or_target_stop_takes_priority_when_both_in_range():
    position = Position(pair="BTCINR", side="buy", qty=1, entry_price=100, stop_loss=90, take_profit=110)
    reason, price = check_stop_or_target(position, candle_high=120, candle_low=80)
    assert reason == "stop_loss"
    assert price == 90


def test_check_stop_or_target_take_profit_when_only_that_in_range():
    position = Position(pair="BTCINR", side="buy", qty=1, entry_price=100, stop_loss=90, take_profit=110)
    reason, price = check_stop_or_target(position, candle_high=115, candle_low=95)
    assert reason == "take_profit"
    assert price == 110


def test_check_stop_or_target_neither_triggered():
    position = Position(pair="BTCINR", side="buy", qty=1, entry_price=100, stop_loss=90, take_profit=110)
    reason, price = check_stop_or_target(position, candle_high=105, candle_low=95)
    assert reason is None and price is None


def test_equity_is_cash_plus_marked_to_market_holdings_and_excludes_tds_credit():
    wallet = _wallet(cash=500.0)
    wallet.tds_credit = 999.0  # must NOT leak into equity -- spec section 8
    position = Position(pair="BTCINR", side="buy", qty=0.01, entry_price=8000000, stop_loss=1, take_profit=1)
    e = equity(wallet, [position], {"BTCINR": 8500000})
    assert e == pytest.approx(500.0 + 0.01 * 8500000)


def test_is_dead_below_threshold():
    wallet = _wallet(starting=1000.0)
    assert is_dead(wallet, current_equity=199.0, death_threshold_pct=20) is True
    assert is_dead(wallet, current_equity=201.0, death_threshold_pct=20) is False
