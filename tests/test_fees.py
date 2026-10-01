from __future__ import annotations

import pytest

from core.fees import apply_slippage, roundtrip_cost_pct, simulate_fill


def test_slippage_makes_buy_worse_and_sell_worse():
    assert apply_slippage(100, "buy", 0.1) == pytest.approx(100.1)
    assert apply_slippage(100, "sell", 0.1) == pytest.approx(99.9)


def test_slippage_rejects_bad_side():
    with pytest.raises(ValueError):
        apply_slippage(100, "short", 0.1)


def test_simulate_fill_buy_has_no_tds_and_negative_cash_delta():
    fill = simulate_fill("buy", qty=1, price=100, fee_pct=0.2, slippage_pct=0.1, tds_pct=1.0)
    assert fill.tds == 0.0
    assert fill.fill_price == pytest.approx(100.1)
    assert fill.fee == pytest.approx(100.1 * 0.002)
    assert fill.cash_delta == pytest.approx(-(100.1 + fill.fee))


def test_simulate_fill_sell_withholds_tds_and_credits_cash():
    fill = simulate_fill("sell", qty=1, price=100, fee_pct=0.2, slippage_pct=0.1, tds_pct=1.0)
    assert fill.fill_price == pytest.approx(99.9)
    notional = 99.9
    expected_fee = notional * 0.002
    expected_tds = notional * 0.01
    assert fill.fee == pytest.approx(expected_fee)
    assert fill.tds == pytest.approx(expected_tds)
    assert fill.cash_delta == pytest.approx(notional - expected_fee - expected_tds)


def test_roundtrip_cost_pct_sums_both_legs():
    pct = roundtrip_cost_pct(taker_fee_pct=0.2, tds_pct=1.0, slippage_pct=0.1)
    assert pct == pytest.approx(2 * 0.2 + 1.0 + 2 * 0.1)
