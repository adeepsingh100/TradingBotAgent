"""Runs exactly one full agent cycle: acquire the tick lock, loop over
every alive agent's wallet, run the LangGraph agent for each, check for
death, record benchmarks/heartbeat, and return a JSON-able summary.
worker/app.py's POST /tick is a thin wrapper around this.

Stateless between calls (spec section 3): every value needed is
queried fresh from the DB at the top of this function, nothing cached
across ticks. All queries below use `.all()` + a Python-side filter
rather than `Query.filter(...)` with SQLAlchemy expressions -- not a
CockroachDB constraint, just keeping every query shape simple enough
for the hand-rolled FakeSession this repo's whole test suite uses
(see tests/test_cycle.py).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core import live_engine
from core.alerts import maybe_send_alert
from core.benchmarks import initialize_benchmarks, record_benchmark_tick
from core.coindcx import client as coindcx_client
from core.config import settings as env
from core.db.models import Agent, AgentMemory, ControlCommand, Decision, EquityHistory, LiveOrder, MarketCache, Position, Setting, Strategy, Trade
from core.db.session import get_session
from core.heartbeat import beat
from core.llm.provider import get_llm
from core.lock import acquire_tick_lock, release_lock
from core.paper_engine import equity, get_open_positions, is_dead, kill_wallet
from core.risk_manager import consecutive_losses
from core.universe import top_inr_symbols

from .agent.graph import build_graph
from .research import DEFAULT_RESEARCH, mark_research_run, research_due, research_one

_GRAPH = build_graph()
_BTC_PAIR_CODE = "I-BTC_INR"


def _apply_pending_resets(session) -> None:
    """`control_commands` (core/db/models.py::ControlCommand) is used
    for exactly one command this repo implements: `reset_wallet` --
    the one dashboard action that isn't a plain field flip (pause/
    resume write `agents.status` directly; the kill switch writes
    `settings.global.kill_switch` directly -- both already re-read
    fresh every tick with no queue needed). A reset wipes a wallet back
    to its starting state: delete every row that references it (FK-safe
    order: trades -> positions -> decisions -> equity_history ->
    agent_memory), drop its benchmark seed so it re-initializes next
    tick, restore cash/tds_credit, and revive its agent."""
    pending = [c for c in session.query(ControlCommand).all() if c.status == "pending" and c.command == "reset_wallet"]
    for command in pending:
        wallet_id = command.wallet_id
        for trade in session.query(Trade).filter_by(wallet_id=wallet_id).all():
            session.delete(trade)
        for position in session.query(Position).filter_by(wallet_id=wallet_id).all():
            session.delete(position)
        for decision in session.query(Decision).filter_by(wallet_id=wallet_id).all():
            session.delete(decision)
        for row in session.query(EquityHistory).filter_by(wallet_id=wallet_id).all():
            session.delete(row)
        for row in session.query(AgentMemory).filter_by(wallet_id=wallet_id).all():
            session.delete(row)
        benchmark_setting = session.query(Setting).filter_by(key=f"benchmark_btc:{wallet_id}").one_or_none()
        if benchmark_setting is not None:
            session.delete(benchmark_setting)

        agent = session.query(Agent).filter_by(wallet_id=wallet_id).one()
        wallet = agent.wallet
        wallet.current_cash = wallet.starting_capital
        wallet.tds_credit = 0.0
        agent.status = "alive"
        agent.died_at = None

        command.status = "applied"
        command.applied_at = datetime.now(timezone.utc)


def _load_settings(session) -> dict:
    return {row.key: row.value for row in session.query(Setting).all()}


def _pair_map(session, watchlist: list[str]) -> dict[str, str]:
    return {
        row.raw.get("symbol"): row.pair
        for row in session.query(MarketCache).all()
        if row.raw.get("symbol") in watchlist
    }


def _market_cache_by_symbol(session, watchlist: list[str]) -> dict[str, MarketCache]:
    """Live-only lookup (core/live_engine.py needs step/min_notional to
    round and validate a real order) -- paper wallets never read this."""
    return {row.raw.get("symbol"): row for row in session.query(MarketCache).all() if row.raw.get("symbol") in watchlist}


def _fetch_btc_price() -> float | None:
    try:
        rows = coindcx_client.get_candles(_BTC_PAIR_CODE, "1h", limit=1)
        return rows[0]["close"] if rows else None
    except Exception:  # noqa: BLE001 -- a benchmark we can't price this tick just skips, never crashes the cycle
        return None


def _auto_watchlist(universe_cfg: dict, fallback: list[str]) -> list[str]:
    """Paper wallets' pairs for this tick, picked from the live ticker
    (core/universe.py). Falls back to the `watchlist` setting only if the
    ticker call fails or nothing survives the filters."""
    try:
        return top_inr_symbols(coindcx_client.get_ticker(), universe_cfg) or fallback
    except Exception:  # noqa: BLE001 -- ticker outage degrades to the fallback list, never crashes the tick
        return fallback


def _maybe_research(session, settings_map, llm, llm_settings, watchlist, risk, costs, candle_interval) -> dict | None:
    """Strategy research (worker/research.py), at most every
    `research.every_hours`. Runs inside a SAVEPOINT so a failure rolls
    back only its own writes -- it must never cost the tick its trades."""
    cfg = {**DEFAULT_RESEARCH, **settings_map.get("research", {})}
    if llm is None or not research_due(session, cfg["every_hours"]):
        return None
    symbols = watchlist[:2] or ["BTCINR"]
    try:
        with session.begin_nested():
            report = research_one(
                session, llm, symbols=symbols, pair_map=_pair_map(session, symbols), interval=candle_interval,
                costs=costs, size_pct=risk.get("max_position_size_pct", 20), cfg=cfg,
                provider=llm_settings.get("provider", env.llm_provider), model=llm_settings.get("model", env.llm_model),
            )
    except Exception as exc:  # noqa: BLE001 -- research is optional; a failed run is retried next window
        report = {"error": str(exc)[:500]}
    mark_research_run(session, report)
    return report


def _wallet_risk_state(session, wallet, risk: dict) -> dict:
    all_trades = session.query(Trade).filter_by(wallet_id=wallet.id).all()
    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    trades_today_count = sum(1 for t in all_trades if t.side == "buy" and t.executed_at >= today_start)
    todays_closed = [t for t in all_trades if t.pnl is not None and t.executed_at >= today_start]
    daily_pnl = sum(t.pnl for t in todays_closed)
    daily_pnl_pct = (daily_pnl / wallet.starting_capital * 100) if wallet.starting_capital else 0.0

    closed_sorted = sorted((t for t in all_trades if t.pnl is not None), key=lambda t: t.executed_at, reverse=True)
    losses_in_a_row = consecutive_losses([t.pnl for t in closed_sorted])
    cooldown_until = None
    if closed_sorted and losses_in_a_row >= risk.get("consecutive_losses_trigger", 3):
        cooldown_until = closed_sorted[0].executed_at + timedelta(hours=risk.get("cooldown_hours_after_losses", 4))

    return {"trades_today_count": trades_today_count, "daily_pnl_pct": daily_pnl_pct, "cooldown_until": cooldown_until}


def _run_wallet_tick(
    session, wallet, agent, pair_map, market_cache, strategies, risk, costs, global_settings,
    live_trading_enabled, llm, llm_settings, death_threshold_pct, candle_interval="1h",
) -> dict:
    open_positions = get_open_positions(session, wallet.id)
    risk_state = _wallet_risk_state(session, wallet, risk)

    state = {
        "session": session, "wallet": wallet, "agent": agent,
        "watchlist": list(pair_map.keys()), "pair_map": pair_map, "market_cache": market_cache,
        "live_trading_enabled": live_trading_enabled,
        "open_positions": open_positions, "strategies": strategies,
        "risk_settings": risk, "costs_settings": costs,
        "equity": 0.0,  # overwritten by check_exits once prices are loaded
        "kill_switch": global_settings.get("kill_switch", False),
        "death_threshold_pct": death_threshold_pct,
        "candle_interval": candle_interval,
        "llm": llm, "llm_provider": llm_settings.get("provider", "nvidia"), "llm_model": llm_settings.get("model", ""),
        "candles": {}, "prices": {}, "strategy_assignment": {}, "signals": {}, "proposals": {}, "results": [],
        **risk_state,
    }
    result_state = _GRAPH.invoke(state)
    return {
        "wallet": wallet.name,
        "equity": equity(wallet, get_open_positions(session, wallet.id), result_state.get("prices", {})),
        "prices": result_state.get("prices", {}),
        "results": result_state.get("results", []),
    }


def run_cycle(holder: str = "worker") -> dict:
    with get_session() as session:
        if not acquire_tick_lock(session, holder=holder):
            return {"skipped": "tick lock held by another run"}

        _apply_pending_resets(session)

        settings_map = _load_settings(session)
        risk = settings_map.get("risk", {})
        # Live wallets never share paper's risk row -- paper's numbers were never
        # vetted against real-money consequences. Falls back to paper's row only
        # until a human has actually configured settings["risk_live"].
        risk_live = settings_map.get("risk_live", risk)
        costs = settings_map.get("costs", {})
        # Paper wallets find their own coins each tick; `watchlist` is only the
        # fallback if the ticker is down. Live stays on its hand-picked list.
        watchlist = _auto_watchlist(settings_map.get("universe", {}), settings_map.get("watchlist", []))
        watchlist_live = settings_map.get("watchlist_live", ["BTCINR"])  # v1: one pair, most liquid, least slippage surprise
        llm_settings = settings_map.get("llm", {})
        global_settings = settings_map.get("global", {})
        live_trading_enabled = settings_map.get("live_trading", {}).get("enabled", False)
        death_threshold_pct = settings_map.get("death_threshold_pct", env.death_threshold_pct)
        candle_interval = settings_map.get("candle_interval", "1h")

        all_strategies = session.query(Strategy).all()
        strategies = {s.type: s for s in all_strategies if s.status != "retired"}
        # A live wallet must only ever see strategies a human has explicitly
        # promoted -- clearing the statistical bar (core/promotion.py) is never
        # sufficient alone (settings.global.require_human_approval_for_live).
        live_strategies = {s.type: s for s in all_strategies if s.status in ("approved_for_live", "live")}

        try:
            llm = get_llm(
                llm_settings.get("provider", env.llm_provider), llm_settings.get("model", env.llm_model),
                {"nvidia_api_key": env.nvidia_api_key, "anthropic_api_key": env.anthropic_api_key, "openai_api_key": env.openai_api_key},
            )
        except Exception:  # noqa: BLE001 -- misconfigured provider degrades this tick to mechanical-signal-only, never crashes the worker
            llm = None

        btc_price = _fetch_btc_price()
        wallet_summaries = []
        for agent in session.query(Agent).all():
            if agent.status != "alive":
                continue
            wallet = agent.wallet
            is_live = wallet.kind == "live"

            # Always include pairs this wallet already holds -- a coin that drops
            # out of the top-N must still get its stop/target checked every tick.
            held = [p.pair for p in get_open_positions(session, wallet.id)]
            wallet_watchlist = list(dict.fromkeys((watchlist_live if is_live else watchlist) + held))
            wallet_risk = risk_live if is_live else risk
            wallet_strategies = live_strategies if is_live else strategies
            pair_map = _pair_map(session, wallet_watchlist)
            market_cache = _market_cache_by_symbol(session, wallet_watchlist)

            benchmark_key = f"benchmark_btc:{wallet.id}"
            just_initialized_benchmarks = False
            if btc_price is not None and session.query(Setting).filter_by(key=benchmark_key).one_or_none() is None:
                initialize_benchmarks(session, wallet, btc_price, costs)
                just_initialized_benchmarks = True  # already wrote this tick's first equity_history point

            summary = _run_wallet_tick(
                session, wallet, agent, pair_map, market_cache, wallet_strategies, wallet_risk, costs,
                global_settings, live_trading_enabled, llm, llm_settings, death_threshold_pct, candle_interval,
            )
            wallet_summaries.append(summary)

            still_open = get_open_positions(session, wallet.id)
            current_equity = equity(wallet, still_open, summary["prices"])
            session.add(EquityHistory(
                wallet_id=wallet.id, series="agent", equity_inr=current_equity,
                cash_inr=wallet.current_cash, holdings_value_inr=current_equity - wallet.current_cash,
                tds_credit_inr=wallet.tds_credit, recorded_at=datetime.now(timezone.utc),
            ))
            if is_dead(wallet, current_equity, death_threshold_pct):
                if is_live:
                    live_engine.kill_wallet(session, wallet, agent, still_open, costs, market_cache, allow_live=live_trading_enabled)
                else:
                    kill_wallet(session, wallet, agent, still_open, summary["prices"], costs)
                maybe_send_alert(
                    session, "agent_died", settings_map,
                    f"Wallet '{wallet.name}' died -- equity {current_equity:.2f} fell below "
                    f"{death_threshold_pct}% of starting capital.",
                    wallet_id=wallet.id,
                )
            elif btc_price is not None and not just_initialized_benchmarks:
                record_benchmark_tick(session, wallet, btc_price)

            if is_live:
                for order in session.query(LiveOrder).filter_by(wallet_id=wallet.id).all():
                    if order.status != "unknown_needs_manual_check":
                        continue
                    maybe_send_alert(
                        session, "live_order_unknown_state", settings_map,
                        f"Wallet '{wallet.name}': live order {order.client_order_id} ({order.pair} {order.side}) "
                        f"is in an unknown state -- check CoinDCX's order history manually (LiveOrder id: {order.id}).",
                        wallet_id=wallet.id,
                    )

        research_report = _maybe_research(session, settings_map, llm, llm_settings, watchlist, risk, costs, candle_interval)

        beat(session, "worker_tick", detail={"wallets": len(wallet_summaries)})
        release_lock(session, holder=holder)  # next minute's /tick can start right away
        return {"wallets": wallet_summaries, **({"research": research_report} if research_report else {})}
