"""No real network/LLM -- candles are synthetic, call_structured and
get_candles are monkeypatched. Covers the parts that decide whether a
strategy actually changes: bounds, the out-of-sample accept rule, and
versioning (new draft + old retired, live-approved never touched)."""

from __future__ import annotations

import math
import uuid

from core.db.models import Backtest, Strategy
from tests.conftest import FakeSession, make_candles
from worker import research
from worker.agent.schemas import ParamCandidate, ParamValue, ResearchOutput

COSTS = {"taker_fee_pct": 0.2, "slippage_pct": 0.1, "tds_pct": 1.0, "gst_pct": 18.0}


def _stats(train, val):
    return {"train": {"net_pnl": train}, "validation": {"net_pnl": val}}


def test_accepts_only_if_better_on_train_and_validation_and_profitable_out_of_sample():
    base = _stats(-50, -10)
    assert research.accepts(_stats(10, 5), base)
    assert not research.accepts(_stats(10, -5), base)    # better than baseline but still losing out of sample
    assert not research.accepts(_stats(-60, 5), base)    # worse on train
    assert not research.accepts(_stats(10, -20), base)   # overfit: train up, validation down


def test_to_params_rounds_ints_and_rejects_out_of_bounds():
    ok = research._to_params("ema_crossover", [ParamValue(name="fast_period", value=9.6), ParamValue(name="slow_period", value=30)])
    assert ok["fast_period"] == 10 and ok["slow_period"] == 30
    assert research._to_params("ema_crossover", [ParamValue(name="fast_period", value=999)]) is None


def _wave(n=600):
    return make_candles([100 + 8 * math.sin(i / 15) for i in range(n)])


def _run(monkeypatch, session, candidates):
    monkeypatch.setattr(research.coindcx_client, "get_candles", lambda pair, interval, limit: list(reversed(_wave())))
    monkeypatch.setattr(research, "call_structured", lambda *a, **kw: ResearchOutput(candidates=candidates))
    return research.research_one(
        session, object(), symbols=["BTCINR"], pair_map={"BTCINR": "I-BTC_INR"}, interval="15m",
        costs=COSTS, size_pct=20, cfg=dict(research.DEFAULT_RESEARCH), provider="nvidia", model="m",
    )


def test_accepted_candidate_becomes_a_new_draft_and_retires_the_old(monkeypatch):
    session = FakeSession()
    old = Strategy(id=uuid.uuid4(), type="ema_crossover", status="draft", stats={},
                   params={"fast_period": 5, "slow_period": 10, "stop_loss_pct": 0.5, "take_profit_pct": 1.0})
    session.add(old)
    good = ParamCandidate(params=[ParamValue(name="fast_period", value=5), ParamValue(name="slow_period", value=20),
                                  ParamValue(name="stop_loss_pct", value=8), ParamValue(name="take_profit_pct", value=10)])
    monkeypatch.setattr(research, "accepts", lambda cand, base: True)

    report = _run(monkeypatch, session, [good])

    assert report["accepted"]["slow_period"] == 20
    assert old.status == "retired"
    new = [s for s in session.query(Strategy).all() if s is not old]
    assert len(new) == 1 and new[0].status == "draft" and new[0].stats["derived_from"] == str(old.id)
    assert len(session.query(Backtest).all()) == 1


def test_rejected_candidate_leaves_the_strategy_alone(monkeypatch):
    session = FakeSession()
    old = Strategy(id=uuid.uuid4(), type="ema_crossover", status="draft", stats={}, params={})
    session.add(old)
    cand = ParamCandidate(params=[ParamValue(name="fast_period", value=20)])
    monkeypatch.setattr(research, "accepts", lambda cand, base: False)

    report = _run(monkeypatch, session, [cand])

    assert report["accepted"] is None and report["tried"] == 1
    assert old.status == "draft" and "last_researched_at" in old.stats
    assert len(session.query(Strategy).all()) == 1


def test_live_approved_strategies_are_never_researched(monkeypatch):
    session = FakeSession()
    session.add(Strategy(id=uuid.uuid4(), type="ema_crossover", status="approved_for_live", stats={}, params={}))

    assert _run(monkeypatch, session, []) == {"skipped": "no researchable strategy"}
