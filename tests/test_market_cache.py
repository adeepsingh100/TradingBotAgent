"""No real DB/network -- refresh_market_cache's DB session is faked,
client.get_markets_details is monkeypatched. The rounding helpers are
pure functions, tested directly."""

from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import MagicMock

from core.coindcx import market_cache


def test_round_quantity_rounds_down_to_step():
    assert market_cache.round_quantity(0.123456, 0.0001) == 0.1234
    assert market_cache.round_quantity(0.1, 0.0001) == 0.1
    assert market_cache.round_quantity(5, 0) == 5  # step=0 (shouldn't happen, but don't crash/divide-by-zero)


def test_round_price_rounds_to_base_precision():
    assert market_cache.round_price(8396518.6789, 1) == 8396518.7
    assert market_cache.round_price(42.12345, 3) == 42.123


def test_meets_min_notional():
    assert market_cache.meets_min_notional(qty=0.01, price=8000000, min_notional=100) is True
    assert market_cache.meets_min_notional(qty=0.00001, price=8000000, min_notional=100) is False


def test_refresh_market_cache_filters_to_inr_spot_and_upserts(monkeypatch):
    fake_details = [
        {"pair": "I-BTC_INR", "ecode": "I", "base_currency_short_name": "INR", "min_quantity": 1e-5,
         "max_quantity": 100, "step": 1e-5, "min_notional": 100, "base_currency_precision": 1,
         "target_currency_precision": 5, "order_types": ["limit_order", "market_order"]},
        {"pair": "B-BTC_USDT", "ecode": "B", "base_currency_short_name": "USDT", "min_quantity": 1e-5,
         "max_quantity": 100, "step": 1e-5, "min_notional": 10, "base_currency_precision": 2,
         "target_currency_precision": 5, "order_types": ["limit_order"]},
    ]
    monkeypatch.setattr(market_cache.client, "get_markets_details", lambda: fake_details)

    added = []
    fake_session = MagicMock()
    fake_session.query.return_value.filter_by.return_value.one_or_none.return_value = None
    fake_session.add.side_effect = lambda obj: added.append(obj)

    @contextmanager
    def fake_get_session():
        yield fake_session

    monkeypatch.setattr(market_cache, "get_session", fake_get_session)

    count = market_cache.refresh_market_cache(only_inr_spot=True)

    assert count == 1  # the USDT pair is filtered out
    assert len(added) == 1
    assert added[0].pair == "I-BTC_INR"
