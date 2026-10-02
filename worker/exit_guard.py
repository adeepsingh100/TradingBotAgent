"""Between-tick exit guard -- checks every open position's stop/target
against the live price every few seconds, no LLM involved. The full
agent tick (LLM calls, minutes per run at worst) runs at most once a
minute from cron; a stop must not wait that long when price can move in
seconds -- especially on a live wallet, whose exit is a market sell at
whatever the price is when it fires.

Runs as a daemon thread inside the worker process (worker/app.py starts
it at boot, worker/run_local.py too). On Render's free plan that only
works because cron-job.org hitting /tick every minute keeps the instance
from sleeping -- if it does sleep, the guard stops until the next /tick
wakes it, and the tick's own check_exits covers that gap.

One pass = one public ticker call for every pair + one short DB
transaction. Concurrency with a running tick:
- Its own lock (`exit_lock`), not `tick_lock` -- a tick holds tick_lock
  for its whole run, LLM calls included, which would blind the guard
  exactly when it's needed.
- `lock_timeout` on the whole pass: if a tick has uncommitted writes on
  the same wallet/position rows, the pass gives up within seconds and
  retries next pass rather than queueing behind the tick.
- If both close the same position at the same instant, CockroachDB's
  SERIALIZABLE isolation aborts one of the two transactions -- never a
  double sell. Rare (both must see the stop in the same few seconds);
  worst case the tick retries a minute later.

Paper fills: a stop hit fills at the WORSE of the stop level and the
observed price (a gap through the stop is a real loss, not a free pass);
a target fills at the target level, never better -- conservative both
ways.
"""

from __future__ import annotations

import logging
import threading
import time

from sqlalchemy import text

from core.coindcx import client as coindcx_client
from core.db.models import MarketCache, Position, Wallet
from core.db.session import get_session
from core.lock import acquire_tick_lock, release_lock
from core.paper_engine import check_stop_or_target

from .agent.nodes import close_triggered
from .cycle import _load_settings

log = logging.getLogger(__name__)

_HOLDER = "exit_guard"
_LOCK = "exit_lock"


def run_exit_pass() -> dict:
    with get_session() as session:
        if not acquire_tick_lock(session, holder=_HOLDER, name=_LOCK, ttl_seconds=60):
            return {"skipped": "exit lock held"}
        session.execute(text("SET LOCAL lock_timeout = '3s'"))  # never queue behind a running tick's writes

        open_positions = session.query(Position).filter_by(closed_at=None).all()
        closed = []
        if open_positions:
            prices = {r["market"]: float(r["last_price"]) for r in coindcx_client.get_ticker() if r.get("last_price")}
            settings_map = _load_settings(session)
            costs = settings_map.get("costs", {})
            allow_live = settings_map.get("live_trading", {}).get("enabled", False)
            for position in open_positions:
                price = prices.get(position.pair)
                if price is None:
                    continue
                reason, exit_price = check_stop_or_target(position, price, price)
                if reason is None:
                    continue
                if reason == "stop_loss":
                    exit_price = min(exit_price, price)
                wallet = session.query(Wallet).filter_by(id=position.wallet_id).one()
                market_cache_row = next(
                    (m for m in session.query(MarketCache).all() if (m.raw or {}).get("symbol") == position.pair), None,
                ) if wallet.kind == "live" else None
                if close_triggered(session, wallet, position, reason, exit_price, costs, market_cache_row, allow_live):
                    closed.append({"wallet": wallet.name, "pair": position.pair, "reason": reason, "price": price})
        release_lock(session, holder=_HOLDER, name=_LOCK)
        return {"checked": len(open_positions), "closed": closed}


def _loop(interval_seconds: float) -> None:
    while True:
        try:
            result = run_exit_pass()
            if result.get("closed"):
                log.info("exit guard closed %s", result["closed"])
        except Exception:  # noqa: BLE001 -- a bad pass (ticker/DB blip, lock timeout) never kills the guard
            log.exception("exit guard pass failed")
        time.sleep(interval_seconds)


def start_exit_guard(interval_seconds: float) -> threading.Thread | None:
    if interval_seconds <= 0:
        return None
    thread = threading.Thread(target=_loop, args=(interval_seconds,), name="exit-guard", daemon=True)
    thread.start()
    return thread
