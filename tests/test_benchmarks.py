"""No real DB -- same FakeSession shape as test_paper_engine.py."""

from __future__ import annotations

import uuid

import pytest

from core.benchmarks import initialize_benchmarks, record_benchmark_tick
from core.db.models import EquityHistory, Setting, Wallet

COSTS = {"maker_fee_pct": 0.2, "taker_fee_pct": 0.2, "tds_pct": 1.0, "slippage_pct": 0.1}


class FakeSession:
    def __init__(self):
        self.added = []

    def add(self, obj):
        self.added.append(obj)

    def query(self, model):
        items = [o for o in self.added if isinstance(o, model)]

        class _Q:
            def filter_by(self, **kw):
                nonlocal items
                items2 = [o for o in items if all(getattr(o, k) == v for k, v in kw.items())]
                return _R(items2)

        class _R:
            def __init__(self, items):
                self._items = items

            def one(self):
                return self._items[0]

        return _Q()


def test_initialize_benchmarks_spends_full_starting_capital_on_btc():
    session = FakeSession()
    wallet = Wallet(id=uuid.uuid4(), name="small", kind="paper", starting_capital=1000.0, current_cash=1000.0, tds_credit=0.0)

    initialize_benchmarks(session, wallet, btc_price=8000000, costs_settings=COSTS)

    setting = next(o for o in session.added if isinstance(o, Setting))
    assert setting.value["entry_price"] == 8000000
    equity_rows = [o for o in session.added if isinstance(o, EquityHistory)]
    assert {r.series for r in equity_rows} == {"benchmark_btc", "benchmark_cash"}
    assert all(r.equity_inr == pytest.approx(1000.0) for r in equity_rows)


def test_record_benchmark_tick_marks_btc_to_new_price_and_keeps_cash_flat():
    session = FakeSession()
    wallet = Wallet(id=uuid.uuid4(), name="small", kind="paper", starting_capital=1000.0, current_cash=1000.0, tds_credit=0.0)
    initialize_benchmarks(session, wallet, btc_price=8000000, costs_settings=COSTS)

    record_benchmark_tick(session, wallet, btc_price=8800000)  # +10% BTC move

    btc_rows = [o for o in session.added if isinstance(o, EquityHistory) and o.series == "benchmark_btc"]
    cash_rows = [o for o in session.added if isinstance(o, EquityHistory) and o.series == "benchmark_cash"]
    assert btc_rows[-1].equity_inr > 1000.0  # up roughly with BTC, after entry costs
    assert cash_rows[-1].equity_inr == pytest.approx(1000.0)
