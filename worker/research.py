"""Self-improving strategies: the agent periodically proposes better
params for its own strategies and keeps only what survives an honest
out-of-sample test. Runs inside a tick (worker/cycle.py), at most once
every `settings.research.every_hours`, one strategy per run (the one
researched longest ago) -- bounds the cost to one LLM call and a few
sub-second backtests per run.

One run, for one strategy:
1. Fetch real candles (the tick's own interval) for the top 2 pairs the
   paper wallets are trading right now.
2. Backtest the CURRENT params once per pair over the full window.
   Trades opened in the first 70% count as "train", the last 30% as
   "validation" -- one full run instead of two sliced ones, so
   indicators keep their warm-up history across the split.
3. Ask the LLM for up to 3 param sets inside each Params class's own
   safe bounds (core/strategies/*.py), given the train results and the
   real round-trip cost. It never sees validation results.
4. Accept the candidate with the best train P&L only if it ALSO beats
   the current params on validation, and is profitable there. Beating
   train alone is how overfitting looks.
5. Accepted -> a NEW `draft` Strategy row with the new params, and the
   old row retired. Never edited in place: core/promotion.py's live
   track record aggregates trades per strategy_id, so mixing two param
   sets under one id would let one set's paper record vouch for the
   other's real money.

Only `draft`/`backtested`/`paper_testing` strategies are ever touched --
anything a human approved for live is frozen.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.backtester import run_backtest
from core.coindcx import client as coindcx_client
from core.db.models import Backtest, Setting, Strategy
from core.llm.provider import call_structured
from core.strategies.registry import STRATEGY_REGISTRY, validate_params

from .agent.schemas import ResearchOutput

RESEARCHABLE = ("draft", "backtested", "paper_testing")
DEFAULT_RESEARCH = {"every_hours": 2, "train_fraction": 0.7, "candles": 1000, "starting_capital": 1000.0}
_STATE_KEY = "research_state"


def _split_stats(result, split_time) -> dict:
    out = {}
    for part, trades in (
        ("train", [t for t in result.trades if t.opened_at < split_time]),
        ("validation", [t for t in result.trades if t.opened_at >= split_time]),
    ):
        reasons: dict[str, int] = {}
        for t in trades:
            reasons[t.exit_reason] = reasons.get(t.exit_reason, 0) + 1
        out[part] = {
            "trades": len(trades), "net_pnl": round(sum(t.pnl for t in trades), 2),
            "wins": sum(t.pnl > 0 for t in trades), "exits": reasons,
        }
    return out


def evaluate(series: dict[str, list[dict]], strategy_type: str, params: dict, cfg: dict, costs: dict, size_pct: float) -> dict:
    """Train/validation P&L summed across pairs, one backtest per pair."""
    total = {"train": {"trades": 0, "net_pnl": 0.0, "wins": 0, "exits": {}}, "validation": {"trades": 0, "net_pnl": 0.0, "wins": 0, "exits": {}}}
    for candles in series.values():
        split_time = candles[int(len(candles) * cfg["train_fraction"])]["time"]
        result = run_backtest(candles, strategy_type, params, starting_capital=cfg["starting_capital"],
                              costs_settings=costs, max_position_size_pct=size_pct)
        for part, stats in _split_stats(result, split_time).items():
            agg = total[part]
            agg["trades"] += stats["trades"]
            agg["net_pnl"] = round(agg["net_pnl"] + stats["net_pnl"], 2)
            agg["wins"] += stats["wins"]
            for k, v in stats["exits"].items():
                agg["exits"][k] = agg["exits"].get(k, 0) + v
    return total


def accepts(candidate: dict, baseline: dict) -> bool:
    return (
        candidate["train"]["net_pnl"] > baseline["train"]["net_pnl"]
        and candidate["validation"]["net_pnl"] > baseline["validation"]["net_pnl"]
        and candidate["validation"]["net_pnl"] > 0
    )


def _to_params(strategy_type: str, pairs) -> dict | None:
    """LLM name/value pairs -> a validated params dict, or None if any
    value is out of the strategy's own safe bounds."""
    params_class, _ = STRATEGY_REGISTRY[strategy_type]
    raw = {}
    for p in pairs:
        field = params_class.model_fields.get(p.name)
        if field is None:
            continue
        raw[p.name] = round(p.value) if field.annotation is int else p.value
    try:
        return validate_params(strategy_type, raw).model_dump()
    except Exception:  # noqa: BLE001 -- out-of-bounds proposal is just discarded
        return None


def _round_trip_cost_pct(costs: dict) -> float:
    fee = costs.get("taker_fee_pct", 0.2) * (1 + costs.get("gst_pct", 0.0) / 100)
    return round(2 * fee + 2 * costs.get("slippage_pct", 0.1) + costs.get("tds_pct", 1.0), 3)


def _prompt(strategy: Strategy, bounds: dict, baseline: dict, costs: dict, interval: str, symbols: list[str]) -> str:
    return (
        "You are improving one of your own trading strategies so you survive -- a strategy that loses money "
        "brings your wallet closer to death. Propose up to 3 DIFFERENT parameter sets likely to make more money "
        "after costs than the current one.\n"
        f"Strategy: {strategy.type}. Candles: {interval} on {', '.join(symbols)}.\n"
        f"Parameter bounds (JSON schema; stay strictly inside min/max): {bounds}\n"
        f"Current params: {strategy.params}\n"
        f"Current params' backtest on recent history: {baseline['train']}\n"
        f"Round-trip cost per trade is about {_round_trip_cost_pct(costs)}% of position size (fees + GST + slippage "
        "+ 1% TDS), so targets must be well above that and fewer, better trades usually beat many small ones.\n"
        "Return each set as a list of {name, value} pairs using the exact parameter names, plus a one-line reason."
    )


def research_one(session, llm, *, symbols: list[str], pair_map: dict[str, str], interval: str,
                 costs: dict, size_pct: float, cfg: dict, provider: str, model: str, fallback=None) -> dict:
    candidates = [s for s in session.query(Strategy).all() if s.status in RESEARCHABLE]
    if not candidates:
        return {"skipped": "no researchable strategy"}
    strategy = min(candidates, key=lambda s: (s.stats or {}).get("last_researched_at", ""))
    now = datetime.now(timezone.utc).isoformat()
    strategy.stats = {**(strategy.stats or {}), "last_researched_at": now}

    series = {}
    for symbol in symbols:
        rows = list(reversed(coindcx_client.get_candles(pair_map[symbol], interval, limit=cfg["candles"])))
        if len(rows) >= 200:
            series[symbol] = rows
    if not series:
        return {"strategy": strategy.type, "skipped": "no candle history"}

    baseline = evaluate(series, strategy.type, strategy.params, cfg, costs, size_pct)
    params_class, _ = STRATEGY_REGISTRY[strategy.type]
    output = call_structured(
        session, llm, ResearchOutput,
        [{"role": "user", "content": _prompt(strategy, params_class.model_json_schema()["properties"], baseline, costs, interval, list(series))}],
        node="research", wallet_id=None, provider=provider, model=model, fallback=fallback,
    )
    tried = []
    for cand in (output.candidates if output else [])[:3]:
        params = _to_params(strategy.type, cand.params)
        if params is None or params == strategy.params:
            continue
        tried.append({"params": params, "result": evaluate(series, strategy.type, params, cfg, costs, size_pct), "reasoning": cand.reasoning})

    report = {"strategy": strategy.type, "baseline": baseline, "tried": len(tried), "accepted": None}
    winner = max(tried, key=lambda t: t["result"]["train"]["net_pnl"], default=None)
    if winner is None or not accepts(winner["result"], baseline):
        return report

    first = min(c[0]["time"] for c in series.values())
    last = max(c[-1]["time"] for c in series.values())
    new = Strategy(
        type=strategy.type, params=winner["params"], status="draft",
        stats={"derived_from": str(strategy.id), "last_researched_at": now, "research": {
            "baseline": baseline, "result": winner["result"], "reasoning": winner["reasoning"],
            "interval": interval, "symbols": list(series),
        }},
    )
    session.add(new)
    session.flush()
    strategy.status = "retired"
    strategy.stats = {**strategy.stats, "retired_reason": f"superseded by {new.id} via research"}
    session.add(Backtest(
        strategy_id=new.id, params_snapshot=winner["params"],
        start_date=datetime.fromtimestamp(first / 1000, timezone.utc), end_date=datetime.fromtimestamp(last / 1000, timezone.utc),
        trade_count=winner["result"]["train"]["trades"] + winner["result"]["validation"]["trades"],
        net_pnl=winner["result"]["train"]["net_pnl"] + winner["result"]["validation"]["net_pnl"],
    ))
    report["accepted"] = winner["params"]
    return report


def research_due(session, every_hours: float) -> bool:
    row = session.query(Setting).filter_by(key=_STATE_KEY).one_or_none()
    last = (row.value or {}).get("last_run_at") if row else None
    if not last:
        return True
    return (datetime.now(timezone.utc) - datetime.fromisoformat(last)).total_seconds() >= every_hours * 3600


def mark_research_run(session, report: dict) -> None:
    value = {"last_run_at": datetime.now(timezone.utc).isoformat(), "last_report": report}
    row = session.query(Setting).filter_by(key=_STATE_KEY).one_or_none()
    if row is None:
        session.add(Setting(key=_STATE_KEY, value=value))
    else:
        row.value = value
