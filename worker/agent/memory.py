"""How the agent learns from its own mistakes, with no extra LLM call:
every closed position writes one plain-text lesson to `agent_memory`
(coin, strategy, win/loss, how it exited, why it entered), and both LLM
prompts open with the wallet's track record -- win/loss per strategy and
per coin from the `trades` table, plus the most recent lessons. The model
draws the conclusions in-context ("ema_crossover keeps getting stopped
out on SOLINR"); nothing here tunes params or blocks a trade itself --
risk_manager stays the only hard gate.

Written on close, not derived at prompt time, because the exit reason
(stop vs. target) and the entry reasoning are only both in hand at that
moment. A wallet reset already wipes `agent_memory` (worker/cycle.py),
so a fresh wallet starts with a clean record.
"""

from __future__ import annotations

from datetime import datetime, timezone

from core.db.models import AgentMemory, Decision, Strategy, Trade

_RECENT_LESSONS = 10


def record_lesson(session, wallet, position, exit_trade, reason: str) -> None:
    strategy = session.query(Strategy).filter_by(id=position.strategy_id).one_or_none() if position.strategy_id else None
    decision = session.query(Decision).filter_by(id=position.decision_id).one_or_none() if position.decision_id else None
    pnl = exit_trade.pnl or 0.0
    notional = position.qty * position.entry_price
    pnl_pct = pnl / notional * 100 if notional else 0.0
    now = datetime.now(timezone.utc)
    held = f" after {(now - position.opened_at).total_seconds() / 3600:.1f}h" if position.opened_at else ""
    entry_reasoning = (decision.proposal or {}).get("reasoning", "") if decision else ""
    content = (
        f"{'WIN' if pnl > 0 else 'LOSS'} {pnl:+.2f} INR ({pnl_pct:+.2f}%) on {position.pair} "
        f"via {strategy.type if strategy else 'unknown strategy'}: bought {position.entry_price:.6g}, "
        f"exited {exit_trade.price:.6g} by {reason}{held}."
        + (f" Why I entered: {entry_reasoning}" if entry_reasoning else "")
    )
    session.add(AgentMemory(wallet_id=wallet.id, strategy_id=position.strategy_id, content=content, created_at=now))


def track_record(session, wallet_id) -> str:
    closed = [t for t in session.query(Trade).filter_by(wallet_id=wallet_id, side="sell").all() if t.pnl is not None]
    if not closed:
        return "Track record: no closed trades yet -- nothing to learn from so far.\n\n"

    names = {s.id: s.type for s in session.query(Strategy).all()}

    def tally(key) -> str:
        groups: dict[str, list[float]] = {}
        for t in closed:
            groups.setdefault(key(t), []).append(t.pnl)
        return "; ".join(
            f"{k} {sum(p > 0 for p in v)}W/{sum(p <= 0 for p in v)}L net {sum(v):+.2f} INR"
            for k, v in sorted(groups.items(), key=lambda kv: sum(kv[1]))  # worst first
        )

    lessons = sorted(
        session.query(AgentMemory).filter_by(wallet_id=wallet_id).all(),
        key=lambda m: m.created_at or datetime.min.replace(tzinfo=timezone.utc), reverse=True,
    )[:_RECENT_LESSONS]
    return (
        "Your track record -- learn from it: lean on what made money, avoid repeating what lost it.\n"
        f"By strategy: {tally(lambda t: names.get(t.strategy_id, 'unknown'))}\n"
        f"By coin: {tally(lambda t: t.pair)}\n"
        "Recent lessons (newest first):\n" + "\n".join(f"- {m.content}" for m in lessons) + "\n\n"
    )
