"""One-time bootstrap: the two default paper wallets + their agents,
and the `settings` table's starting values for every knob the
dashboard's Controls & Settings page can later change live.

Run manually after migrations: python -m scripts.seed_wallets
Safe to re-run -- skips anything that already exists by name/key.
"""

from __future__ import annotations

from core.config import settings as env
from core.db.models import Agent, Setting, Wallet
from core.db.session import get_session
from core.running_cost import DEFAULT_RUNNING_COST
from core.universe import DEFAULT_UNIVERSE
from worker.research import DEFAULT_RESEARCH

DEFAULT_WALLETS = [
    ("small", env.wallet_small_starting_capital),
    ("large", env.wallet_large_starting_capital),
]

# Every value here is a *default*, not a constant -- the dashboard's
# Controls & Settings page edits these rows live, and the worker reads
# them fresh every tick (spec section 3). core/config.py only holds
# what can't safely live in a dashboard-editable table (secrets, DB URL).
DEFAULT_SETTINGS = {
    "risk": {
        "max_position_size_pct": 20,
        "max_risk_per_trade_pct": 3,
        "daily_loss_limit_pct": 8,
        "max_open_positions": 2,
        "max_trades_per_day": 4,
        "min_reward_to_cost_multiple": 1.5,
        "min_confidence": 0.6,
        "cooldown_hours_after_losses": 4,
        "consecutive_losses_trigger": 3,
    },
    "costs": {
        # Base-tier spot %, UNVERIFIED against an official CoinDCX source
        # (coindcx.com/fees 403'd the verification pass) -- confirm from
        # your account's own Fees page and correct here before going live.
        "maker_fee_pct": 0.2,
        "taker_fee_pct": 0.2,
        "tds_pct": 1.0,
        "slippage_pct": 0.1,
        # GST on the trading fee itself (India: 18%), not on notional --
        # core/fees.py::simulate_fill applies this to base fee -- the real
        # per-trade fee CoinDCX charges is fee_pct * (1 + gst_pct/100).
        "gst_pct": 18.0,
    },
    # Paper wallets pick their own pairs every tick from the live ticker
    # (core/universe.py); `watchlist` is only the fallback if that call fails.
    # Daily "rent" every alive wallet pays to keep running (core/running_cost.py)
    # -- 0.5%/day of each stake: 50 on the 10,000 paper wallets, 5 on the
    # 1,000 live wallet. Doing nothing slowly kills it.
    "running_cost": DEFAULT_RUNNING_COST,
    "universe": DEFAULT_UNIVERSE,
    # Candle interval every strategy reads (and research backtests on).
    # "15m" is supported but, backtested 2026-10-02 on BTC/ETH, every
    # strategy lost 4-7x faster per day on 15m than 1h (more trades, same
    # ~1.7% round-trip cost) -- switch only once research finds 15m params
    # that survive out of sample.
    "candle_interval": "1h",
    # Self-improving strategies (worker/research.py): one strategy per run,
    # at most every `every_hours`, accepted only if better out of sample.
    "research": DEFAULT_RESEARCH,
    "watchlist": ["BTCINR", "ETHINR", "SOLINR"],
    # Live wallets never trade paper's full watchlist or risk profile --
    # v1 is deliberately one pair, tighter caps (Phase 7 report). Falls back
    # to the "risk" row above if unset; worker/cycle.py reads this directly.
    "watchlist_live": ["BTCINR"],
    "risk_live": {
        "max_position_size_pct": 10,
        "max_risk_per_trade_pct": 1,
        "daily_loss_limit_pct": 4,
        "max_open_positions": 1,
        "max_trades_per_day": 2,
        "min_reward_to_cost_multiple": 1.5,
        "min_confidence": 0.75,
        "cooldown_hours_after_losses": 8,
        "consecutive_losses_trigger": 2,
    },
    "live_trading": {"enabled": False},
    "promotion": {
        # Loose bar: draft -> backtested is "worth trying in paper",
        # not "worth real money" -- core/promotion.py::clears_backtest_bar.
        "backtest_bar": {
            "min_trades": 5,
            "min_net_pnl": 0,
            "min_profit_factor": 1.0,
            "max_drawdown_pct": 25,
        },
        # Strict bar, spec section 5's exact numbers --
        # core/promotion.py::clears_live_promotion. Clearing this still
        # requires manual approval in the UI either way (spec section 5:
        # require_human_approval_for_live, not changeable from the UI).
        "live_criteria": {
            "min_trades": 20,
            "min_days": 14,
            "min_profit_factor": 1.2,
            "max_drawdown_pct": 15,
        },
    },
    "llm": {"provider": env.llm_provider, "model": env.llm_model},
    "telegram_alerts": {
        "trade_executed": True,
        "strategy_ready_for_live": True,
        "daily_loss_limit_hit": True,
        "cooldown_triggered": True,
        "heartbeat_missing": True,
        "repeated_llm_failures": True,
        "agent_died": True,
        "daily_summary": True,
        # Phase 7 -- live_order_unknown_state is the critical escalation for
        # core/live_engine.py's unresolvable-fill-ambiguity case; note
        # core/alerts.py::maybe_send_alert already defaults an unmapped
        # alert_type to True, so it fires even on a DB without this key yet.
        "live_order_unknown_state": True,
        "live_trading_enabled": True,
        "live_trading_disabled": True,
    },
    "global": {
        "kill_switch": False,
        "mode": "paper",
        # Hard-enforced in code regardless of this value (spec section 5)
        # -- stored for visibility in the UI, not as the actual gate.
        "require_human_approval_for_live": True,
    },
    "death_threshold_pct": env.death_threshold_pct,
}


def _seed_wallet(session, name: str, starting_capital: float) -> None:
    existing = session.query(Wallet).filter_by(name=name).one_or_none()
    if existing is not None:
        print(f"wallet '{name}' already exists, skipping")
        return
    wallet = Wallet(name=name, kind="paper", starting_capital=starting_capital, current_cash=starting_capital)
    session.add(wallet)
    session.flush()
    session.add(Agent(wallet_id=wallet.id, status="alive", mode="paper"))
    print(f"seeded wallet '{name}' (capital={starting_capital}) + its agent")


def _seed_settings(session) -> None:
    for key, value in DEFAULT_SETTINGS.items():
        existing = session.query(Setting).filter_by(key=key).one_or_none()
        if existing is not None:
            print(f"setting '{key}' already exists, skipping")
            continue
        session.add(Setting(key=key, value=value))
        print(f"seeded setting '{key}'")


def main() -> None:
    with get_session() as session:
        for name, capital in DEFAULT_WALLETS:
            _seed_wallet(session, name, capital)
        _seed_settings(session)


if __name__ == "__main__":
    main()
