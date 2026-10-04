"""Graph nodes, in pipeline order: load_market -> check_exits ->
strategize -> generate_mechanical_signals -> decide -> risk_and_execute.

Design decision worth flagging: `decide` lets the LLM set only
`action`/`size_inr`/`confidence`/`reasoning`. `risk_and_execute` always
overwrites `pair`/`entry`/`stop_loss`/`take_profit`/`strategy_id` from
the mechanical strategy signal and the live price, never from whatever
the LLM returned in those fields -- a money-handling path has no
reason to trust an LLM to recall price levels it was already handed,
only to trust it with the smaller judgment call of whether/how much to
size a trade someone else already found. Exits are never LLM-gated at
all (check_exits is pure mechanical stop/target, like paper_engine
everywhere else) -- same reasoning AI-Trader's own README documents
for why it removed its LLM signal-validation gate after an outage;
Survivor's LLM involvement is deliberately confined to sizing/entry
judgment on the one leg (entries) where a bad call only risks capital
already budgeted by the risk manager, never to exits where a stuck
LLM call would leave a position unprotected.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core import live_engine
from core.coindcx import client
from core.db.models import Decision
from core.llm.provider import call_structured
from core.paper_engine import check_stop_or_target, close_position, equity, open_position
from core.risk_manager import Proposal, RiskContext, evaluate
from core.running_cost import runway_days
from core.strategies.registry import generate_signal

from .memory import record_lesson, track_record
from .schemas import StrategizeOutput


def load_market(state: dict) -> dict:
    candles, prices = {}, {}
    for symbol, pair_code in state["pair_map"].items():
        rows = client.get_candles(pair_code, state.get("candle_interval", "1h"), limit=100)
        rows = list(reversed(rows))  # API returns newest-first; every consumer here needs oldest-first
        candles[symbol] = rows
        if rows:
            prices[symbol] = rows[-1]["close"]
    return {"candles": candles, "prices": prices}


def close_triggered(
    session, wallet, position, reason: str, exit_price: float, costs: dict, market_cache_row, allow_live: bool,
) -> bool:
    """Closes one position whose stop/target fired, records the lesson,
    and says whether it actually closed. Shared by the tick's
    `check_exits` and the between-tick exit guard (worker/exit_guard.py)
    so both exit paths behave identically."""
    if wallet.kind == "live":
        if market_cache_row is None:
            return False  # can't size a live sell without it -- leave open, retry next pass
        trade = live_engine.close_position(
            session, wallet, position, costs_settings=costs, market_cache_row=market_cache_row, allow_live=allow_live,
        )
        if trade is None or position.closed_at is None:
            return False  # unresolved (partial fill/timeout/error) -- stays open, retried next pass
    else:
        trade = close_position(session, wallet, position, exit_price=exit_price, costs_settings=costs)
    try:
        record_lesson(session, wallet, position, trade, reason)
    except Exception:  # noqa: BLE001 -- a lost lesson must never block the remaining exits
        pass
    return True


def check_exits(state: dict) -> dict:
    """Mechanical stop/target check -- `check_stop_or_target` itself is
    reused verbatim for both paper and live wallets (it's pure, no
    reason to duplicate it). What happens AFTER it fires branches on
    wallet kind: paper simulates the exit at the candle's stop/target
    level; live submits a real market sell (`core.live_engine
    .close_position`), whose actual fill price will legitimately differ
    slightly from `exit_price` (real slippage) -- `exit_price` here is
    only ever used as the simulated-fill price for paper, never passed
    to the live path."""
    session, wallet, costs = state["session"], state["wallet"], state["costs_settings"]
    closed_pairs = []
    for position in state["open_positions"]:
        rows = state["candles"].get(position.pair)
        if not rows:
            continue
        last = rows[-1]
        reason, exit_price = check_stop_or_target(position, last["high"], last["low"])
        if reason is None:
            continue

        if close_triggered(
            session, wallet, position, reason, exit_price, costs,
            state.get("market_cache", {}).get(position.pair), state.get("live_trading_enabled", False),
        ):
            closed_pairs.append(position.pair)

    remaining = [p for p in state["open_positions"] if p.pair not in closed_pairs]
    # Net of running cost (core/running_cost.py) -- what risk sizing and the prompt should see.
    current_equity = equity(wallet, remaining, state["prices"]) - state.get("rent_paid_inr", 0.0)
    return {"open_positions": remaining, "equity": current_equity}


def _survival_brief(state: dict) -> str:
    """Opening lines of every LLM prompt: how much money is left, where
    death is, and that hold is a valid way to survive -- so the model
    weighs each trade against its own survival, not just the signal."""
    wallet = state["wallet"]
    start = wallet.starting_capital
    eq = state["equity"]
    death_line = start * state.get("death_threshold_pct", 0.0) / 100
    pnl_pct = (eq - start) / start * 100 if start else 0.0
    return (
        "You are Survivor, an autonomous crypto trading agent on CoinDCX (INR spot). "
        "You started with a fixed amount of money and must make money to stay alive.\n"
        f"Started with {start:.2f} INR. You now have {eq:.2f} INR ({pnl_pct:+.2f}%), "
        f"of which {wallet.current_cash:.2f} INR is cash. Open positions: {len(state.get('open_positions', []))}. "
        f"Today's P&L: {state.get('daily_pnl_pct', 0.0):+.2f}%.\n"
        f"If your equity falls below {death_line:.2f} INR you die permanently -- "
        f"you are {eq - death_line:.2f} INR away from death.\n"
        + _rent_lines(state, eq, death_line)
        + "Every trade costs fees, TDS and slippage, so only take trades whose edge clearly beats those costs. "
        "A bad trade brings death closer; holding cash avoids trading losses, but the running cost keeps "
        "draining you every day, so doing nothing forever is also a slow death.\n\n"
    )


def _rent_lines(state: dict, eq: float, death_line: float) -> str:
    daily = state.get("daily_cost_inr", 0.0)
    if daily <= 0:
        return ""
    days = runway_days(eq, death_line, daily)
    return (
        f"Running costs: you pay {daily:.2f} INR every day just to stay alive "
        f"({state.get('rent_paid_inr', 0.0):.2f} INR paid so far; already subtracted from the money above).\n"
        f"At this burn rate you die in about {days} days even if you never lose a trade -- you must earn MORE "
        f"than {daily:.2f} INR per day after fees and TDS to survive.\n"
    )


def strategize(state: dict) -> dict:
    """One LLM call per wallet per tick (not per pair) -- picks which
    strategy TYPE best fits each candidate pair's current conditions.
    Never tunes params (those stay at whatever scripts/seed_strategies.py
    seeded); picking among the 5 already-registered types is the whole
    job here."""
    open_pairs = {p.pair for p in state["open_positions"]}
    candidate_pairs = [s for s in state["watchlist"] if s not in open_pairs]
    llm = state.get("llm")
    if not candidate_pairs or llm is None:
        return {"strategy_assignment": {}}

    context_lines = []
    for symbol in candidate_pairs:
        rows = state["candles"].get(symbol, [])
        if len(rows) < 20:
            continue
        recent = rows[-20:]
        change_pct = (recent[-1]["close"] - recent[0]["close"]) / recent[0]["close"] * 100
        context_lines.append(f"{symbol}: last_price={recent[-1]['close']:.2f}, recent_change={change_pct:.2f}%")
    if not context_lines:
        return {"strategy_assignment": {}}

    strategy_types = sorted(state["strategies"].keys())
    prompt = _survival_brief(state) + track_record(state["session"], state["wallet"].id) + (
        "Choose ONE trading strategy type per pair for this tick, based on its recent trend/volatility.\n"
        f"Available strategy types: {strategy_types}\n"
        "Recent market context:\n" + "\n".join(context_lines) + "\n"
        "Only include pairs you have a clear opinion on; omit the rest."
    )
    result = call_structured(
        state["session"], llm, StrategizeOutput, [{"role": "user", "content": prompt}],
        node="strategize", wallet_id=state["wallet"].id, provider=state["llm_provider"], model=state["llm_model"],
        fallback=state.get("llm_fallback"),
        think=False,  # a 5-way pick per pair; reasoning here only ever ran into the token cap
    )
    if result is None:
        return {"strategy_assignment": {}}

    assignment = {
        a.pair: a.strategy_type
        for a in result.assignments
        if a.pair in candidate_pairs and a.strategy_type in state["strategies"]
    }
    return {"strategy_assignment": assignment}


def generate_mechanical_signals(state: dict) -> dict:
    signals = {}
    for symbol, strategy_type in state["strategy_assignment"].items():
        strategy = state["strategies"][strategy_type]
        rows = state["candles"].get(symbol)
        if not rows:
            continue
        signal = generate_signal(strategy_type, rows, strategy.params, has_position=False)
        signals[symbol] = (strategy, signal)
    return {"signals": signals}


def decide(state: dict) -> dict:
    """Only called for pairs whose mechanical signal is already `buy`
    -- a mechanical `hold` never costs an LLM call."""
    llm = state.get("llm")
    proposals = {}
    for symbol, (strategy, signal) in state["signals"].items():
        if signal.action != "buy":
            continue
        if llm is None:
            proposals[symbol] = None
            continue
        price = state["prices"][symbol]
        prompt = _survival_brief(state) + track_record(state["session"], state["wallet"].id) + (
            f"Strategy '{strategy.type}' generated a BUY signal for {symbol} at price {price}.\n"
            f"Proposed stop_loss={signal.stop_loss}, take_profit={signal.take_profit}.\n"
            f"Strategy reasoning: {signal.reasoning}\n"
            f"Wallet equity: {state['equity']:.2f} INR. Max position size: "
            f"{state['risk_settings'].get('max_position_size_pct')}% of equity.\n"
            "Decide whether to take this trade. Use the given entry/stop_loss/take_profit exactly as "
            "provided -- you only set action ('buy' or 'hold'), size_inr, confidence (0-1), and reasoning."
        )
        proposals[symbol] = call_structured(
            state["session"], llm, Proposal, [{"role": "user", "content": prompt}],
            node="decide", wallet_id=state["wallet"].id, provider=state["llm_provider"], model=state["llm_model"],
            fallback=state.get("llm_fallback"),
        )
    return {"proposals": proposals}


def risk_and_execute(state: dict) -> dict:
    session, wallet = state["session"], state["wallet"]
    results = []
    for symbol, (strategy, signal) in state["signals"].items():
        if signal.action != "buy":
            session.add(Decision(
                wallet_id=wallet.id, strategy_id=strategy.id,
                proposal={"action": "hold", "pair": symbol, "reasoning": signal.reasoning},
                risk_verdict="hold", risk_reason="mechanical strategy signal was not a buy",
            ))
            results.append({"pair": symbol, "verdict": "hold", "reason": "no buy signal"})
            continue

        proposal = state["proposals"].get(symbol) or Proposal(
            action="hold", pair=symbol, reasoning="no LLM decision available -- held"
        )
        proposal.pair = symbol
        proposal.entry = state["prices"][symbol]
        proposal.stop_loss = signal.stop_loss
        proposal.take_profit = signal.take_profit
        proposal.strategy_id = str(strategy.id)

        ctx = RiskContext(
            equity=state["equity"], open_positions_count=len(state["open_positions"]),
            trades_today_count=state["trades_today_count"], daily_pnl_pct=state["daily_pnl_pct"],
            cooldown_until=state["cooldown_until"], now=datetime.now(timezone.utc),
            kill_switch=state["kill_switch"], risk_settings=state["risk_settings"], costs_settings=state["costs_settings"],
        )
        verdict = evaluate(proposal, ctx)

        decision = Decision(
            wallet_id=wallet.id, strategy_id=strategy.id, proposal=proposal.model_dump(),
            risk_verdict=verdict.verdict, risk_reason=verdict.reason,
        )
        session.add(decision)
        session.flush()

        if verdict.verdict in ("approved", "downsized"):
            if wallet.kind == "live":
                market_cache_row = state["market_cache"].get(symbol)
                position = (
                    live_engine.open_position(
                        session, wallet, pair=symbol, qty=verdict.approved_qty, price_hint=proposal.entry,
                        stop_loss=proposal.stop_loss, take_profit=proposal.take_profit,
                        costs_settings=state["costs_settings"], market_cache_row=market_cache_row,
                        strategy_id=strategy.id, decision_id=decision.id, allow_live=state["live_trading_enabled"],
                    )
                    if market_cache_row is not None
                    else None
                )
            else:
                position = open_position(
                    session, wallet, pair=symbol, qty=verdict.approved_qty, entry_price=proposal.entry,
                    stop_loss=proposal.stop_loss, take_profit=proposal.take_profit,
                    costs_settings=state["costs_settings"], strategy_id=strategy.id, decision_id=decision.id,
                )
            if position is not None:
                session.flush()
                decision.executed_trade_id = position.id

        results.append({"pair": symbol, "verdict": verdict.verdict, "reason": verdict.reason})
    return {"results": results}
