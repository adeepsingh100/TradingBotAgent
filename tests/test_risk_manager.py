from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.risk_manager import Proposal, RiskContext, consecutive_losses, evaluate

RISK = {
    "max_position_size_pct": 20,
    "max_risk_per_trade_pct": 3,
    "daily_loss_limit_pct": 8,
    "max_open_positions": 2,
    "max_trades_per_day": 4,
    "min_reward_to_cost_multiple": 1.5,
    "min_confidence": 0.6,
}
COSTS = {"maker_fee_pct": 0.2, "taker_fee_pct": 0.2, "tds_pct": 1.0, "slippage_pct": 0.1}


def _ctx(**overrides) -> RiskContext:
    defaults = dict(
        equity=10000.0,
        open_positions_count=0,
        trades_today_count=0,
        daily_pnl_pct=0.0,
        cooldown_until=None,
        now=datetime(2026, 1, 1, tzinfo=timezone.utc),
        kill_switch=False,
        risk_settings=RISK,
        costs_settings=COSTS,
    )
    defaults.update(overrides)
    return RiskContext(**defaults)


def _good_buy(**overrides) -> Proposal:
    defaults = dict(action="buy", pair="BTCINR", entry=8000000, stop_loss=7900000, take_profit=8300000, confidence=0.8)
    defaults.update(overrides)
    return Proposal(**defaults)


def test_hold_passes_through():
    assert evaluate(Proposal(action="hold", pair="BTCINR"), _ctx()).verdict == "hold"


def test_sell_is_always_approved_regardless_of_other_limits():
    verdict = evaluate(Proposal(action="sell", pair="BTCINR"), _ctx(kill_switch=True, daily_pnl_pct=-50))
    assert verdict.verdict == "approved"


def test_kill_switch_rejects_a_buy():
    assert evaluate(_good_buy(), _ctx(kill_switch=True)).verdict == "rejected"


def test_cooldown_rejects_a_buy():
    future = datetime(2026, 1, 2, tzinfo=timezone.utc)
    ctx = _ctx(now=datetime(2026, 1, 1, tzinfo=timezone.utc), cooldown_until=future)
    assert evaluate(_good_buy(), ctx).verdict == "rejected"


def test_daily_loss_limit_rejects_a_buy():
    assert evaluate(_good_buy(), _ctx(daily_pnl_pct=-8.5)).verdict == "rejected"


def test_max_open_positions_rejects_a_buy():
    assert evaluate(_good_buy(), _ctx(open_positions_count=2)).verdict == "rejected"


def test_max_trades_per_day_rejects_a_buy():
    assert evaluate(_good_buy(), _ctx(trades_today_count=4)).verdict == "rejected"


def test_low_confidence_rejects_a_buy():
    assert evaluate(_good_buy(confidence=0.3), _ctx()).verdict == "rejected"


def test_missing_stop_loss_rejects_a_buy():
    verdict = evaluate(_good_buy(stop_loss=None), _ctx())
    assert verdict.verdict == "rejected"
    assert "stop-loss" in verdict.reason


def test_reward_to_cost_ratio_too_thin_rejects_a_buy():
    # target only 0.1% away -- won't clear 1.5x round-trip costs (~1.5% here)
    verdict = evaluate(_good_buy(take_profit=8008000), _ctx())
    assert verdict.verdict == "rejected"
    assert "round-trip" in verdict.reason


def test_a_clean_buy_within_every_limit_is_approved():
    verdict = evaluate(_good_buy(size_inr=500), _ctx())
    assert verdict.verdict == "approved"
    assert verdict.approved_size_inr == pytest.approx(500)
    assert verdict.approved_qty == pytest.approx(500 / 8000000)


def test_oversized_proposal_gets_downsized_to_the_position_cap():
    # equity=10000, max_position_size_pct=20% -> cap is 2000 INR
    verdict = evaluate(_good_buy(size_inr=5000), _ctx())
    assert verdict.verdict == "downsized"
    assert verdict.approved_size_inr == pytest.approx(2000)


def test_wide_stop_gets_downsized_by_risk_per_trade_cap():
    # risk_per_trade=3% of 10000=300 INR budget; stop_distance huge -> tiny derived size
    verdict = evaluate(_good_buy(size_inr=2000, stop_loss=4000000), _ctx())  # stop_distance = 4,000,000
    risk_budget = 10000 * 0.03
    expected_size = (risk_budget / 4000000) * 8000000
    assert verdict.verdict == "downsized"
    assert verdict.approved_size_inr == pytest.approx(expected_size)


def test_consecutive_losses_stops_at_first_winner():
    assert consecutive_losses([-10, -20, -5, 100, -999]) == 3
    assert consecutive_losses([50, -10]) == 0
    assert consecutive_losses([]) == 0
