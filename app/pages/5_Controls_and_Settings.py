import sys
import uuid
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

# st.set_page_config() must be the very first Streamlit command -- see Home.py's comment
st.set_page_config(page_title="Controls & Settings - Survivor", page_icon="⚙️", layout="wide")

from app.lib.bootstrap import ensure_env_from_secrets

ensure_env_from_secrets()

from app.lib.auth import current_email, require_login, sign_out
from app.lib.queries import list_wallets
from core.alerts import maybe_send_alert
from core.db.models import ControlCommand, Setting, Strategy
from core.db.session import get_session
from core.promotion import clears_live_promotion, gather_live_promotion_stats
from core.running_cost import DEFAULT_RUNNING_COST

email = require_login()
st.sidebar.write(f"Signed in as {email}")
if st.sidebar.button("Sign out"):
    sign_out()
    st.rerun()

st.title("Controls & Settings")
st.caption("Everything here is re-read fresh by the worker every tick (core/config.py's config/settings split) -- no redeploy needed.")


def _setting(session, key: str, default):
    row = session.query(Setting).filter_by(key=key).one_or_none()
    return row, (row.value if row is not None else default)


def _save_setting(session, key: str, value) -> None:
    row = session.query(Setting).filter_by(key=key).one_or_none()
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def _commit_and_rerun(session) -> None:
    """Commit BEFORE st.rerun(). st.rerun() raises RerunException -- a
    BaseException, not an Exception -- so get_session()'s commit (which
    only runs when the `with` block exits normally) is skipped and the
    session closes with the write discarded. Found live: "Confirm enable"
    on live trading (and every other save-then-rerun here) never stuck."""
    session.commit()
    st.rerun()


with get_session() as session:
    # --- Global kill switch ---
    st.subheader("Kill switch")
    global_row, global_settings = _setting(session, "global", {"kill_switch": False, "mode": "paper", "require_human_approval_for_live": True})
    kill_switch = st.toggle("Kill switch (blocks every new entry, every wallet)", value=global_settings.get("kill_switch", False))
    if kill_switch != global_settings.get("kill_switch", False):
        _save_setting(session, "global", {**global_settings, "kill_switch": kill_switch})
        st.success("Kill switch updated.")
        _commit_and_rerun(session)
    st.caption("Blocks every new entry (paper AND live) the instant it's on. Never blocks an exit -- closing risk is never gated.")

    st.divider()

    # --- Live trading enable ---
    st.subheader("Live trading")
    live_row, live_trading = _setting(session, "live_trading", {"enabled": False})
    _, telegram_alerts_for_ping = _setting(session, "telegram_alerts", {})
    if live_trading.get("enabled"):
        st.success(f"Live trading is ON (enabled by {live_trading.get('enabled_by', '?')} at {live_trading.get('enabled_at', '?')}).")
        if st.button("Turn OFF live trading"):
            _save_setting(session, "live_trading", {"enabled": False})
            maybe_send_alert(session, "live_trading_disabled", {"telegram_alerts": telegram_alerts_for_ping},
                              f"Live trading turned OFF by {email}.")
            st.success("Live trading turned off -- open live positions still exit normally, only new entries are blocked.")
            _commit_and_rerun(session)
    else:
        st.info("Live trading is OFF. No real order will ever be placed while this is off, regardless of any wallet's status.")
        with st.popover("Turn ON live trading..."):
            st.warning(
                "This allows REAL orders with REAL money on any wallet with kind='live' that is alive/resumed. "
                "Only strategies promoted below are ever eligible. Confirm you understand this is real capital at risk."
            )
            typed = st.text_input("Type ENABLE LIVE TRADING to confirm", key="enable_live_confirm")
            if st.button("Confirm enable", key="enable_live_btn", disabled=(typed != "ENABLE LIVE TRADING")):
                _save_setting(session, "live_trading", {"enabled": True, "enabled_by": email, "enabled_at": datetime.now(ZoneInfo("Asia/Kolkata")).isoformat(timespec="seconds")})
                maybe_send_alert(session, "live_trading_enabled", {"telegram_alerts": telegram_alerts_for_ping},
                                  f"Live trading turned ON by {email}.")
                st.success("Live trading enabled.")
                _commit_and_rerun(session)

    st.divider()

    # --- Promote strategies to live-eligible ---
    st.subheader("Promote strategies to live")
    promotion_row, promotion_settings = _setting(session, "promotion", {})
    live_criteria = promotion_settings.get("live_criteria", {"min_trades": 20, "min_days": 14, "min_profit_factor": 1.2, "max_drawdown_pct": 15})
    for strategy in session.query(Strategy).order_by(Strategy.type).all():
        if strategy.status in ("approved_for_live", "live"):
            st.caption(f"**{strategy.type}**: already `{strategy.status}`.")
            continue
        stats = gather_live_promotion_stats(session, strategy.id)
        if stats is None:
            st.caption(f"**{strategy.type}**: no closed paper trades yet.")
            continue
        benchmark_return_pct = stats.pop("benchmark_return_pct")
        passed, reasons = clears_live_promotion(stats, live_criteria, benchmark_return_pct)
        if not passed:
            st.caption(f"**{strategy.type}**: doesn't clear the bar yet ({'; '.join(reasons)}).")
            continue
        with st.popover(f"Promote {strategy.type} to live..."):
            st.warning(f"This makes '{strategy.type}' eligible for real-money trades on any live wallet. It cleared every criterion: {stats}")
            typed = st.text_input(f"Type {strategy.type} to confirm", key=f"promote_confirm_{strategy.id}")
            if st.button("Confirm promotion", key=f"promote_btn_{strategy.id}", disabled=(typed != strategy.type)):
                strategy.status = "approved_for_live"
                st.success(f"'{strategy.type}' promoted to approved_for_live.")
                _commit_and_rerun(session)

    st.divider()

    # --- Per-wallet pause/resume/reset ---
    st.subheader("Wallets")
    for wallet in list_wallets(session):
        agent = wallet.agent
        cols = st.columns([2, 2, 2, 2, 3])
        cols[0].write(f"**{wallet.name}** (`{wallet.kind}`)")
        cols[1].write(f"status: `{agent.status if agent else 'unknown'}`")

        if agent and agent.status == "alive":
            if cols[2].button("Pause", key=f"pause_{wallet.id}"):
                agent.status = "paused"
                _commit_and_rerun(session)
        elif agent and agent.status == "paused":
            if cols[2].button("Resume", key=f"resume_{wallet.id}"):
                agent.status = "alive"
                _commit_and_rerun(session)
        else:
            cols[2].caption("dead -- reset to revive")

        if wallet.kind == "live":
            cols[3].caption("Reset disabled for live wallets -- a live wallet needing one is a red flag for manual DB/exchange reconciliation, not a self-serve button.")
        else:
            with cols[3].popover("Reset..."):
                st.warning(f"This wipes ALL of {wallet.name}'s trades/positions/decisions/equity history and restores starting capital. Cannot be undone.")
                typed = st.text_input("Type the wallet name to confirm", key=f"reset_confirm_{wallet.id}")
                if st.button("Confirm reset", key=f"reset_btn_{wallet.id}", disabled=(typed != wallet.name)):
                    session.add(ControlCommand(id=uuid.uuid4(), wallet_id=wallet.id, command="reset_wallet", payload={}, status="pending"))
                    st.success("Reset queued -- the worker applies it on its next tick.")
                    _commit_and_rerun(session)

    st.divider()

    # --- Risk settings ---
    st.subheader("Risk")
    risk_row, risk = _setting(session, "risk", {})
    with st.form("risk_form"):
        c1, c2, c3 = st.columns(3)
        max_position_size_pct = c1.number_input("Max position size %", value=float(risk.get("max_position_size_pct", 20)), min_value=1.0, max_value=100.0)
        max_risk_per_trade_pct = c1.number_input("Max risk per trade %", value=float(risk.get("max_risk_per_trade_pct", 3)), min_value=0.1, max_value=50.0)
        daily_loss_limit_pct = c1.number_input("Daily loss limit %", value=float(risk.get("daily_loss_limit_pct", 8)), min_value=1.0, max_value=100.0)
        max_open_positions = c2.number_input("Max open positions", value=int(risk.get("max_open_positions", 2)), min_value=1, max_value=20, step=1)
        max_trades_per_day = c2.number_input("Max trades per day", value=int(risk.get("max_trades_per_day", 4)), min_value=1, max_value=100, step=1)
        min_reward_to_cost_multiple = c2.number_input("Min reward:cost multiple", value=float(risk.get("min_reward_to_cost_multiple", 1.5)), min_value=1.0, max_value=10.0)
        min_confidence = c3.number_input("Min LLM confidence", value=float(risk.get("min_confidence", 0.6)), min_value=0.0, max_value=1.0)
        cooldown_hours_after_losses = c3.number_input("Cooldown hours after losses", value=float(risk.get("cooldown_hours_after_losses", 4)), min_value=0.0, max_value=72.0)
        consecutive_losses_trigger = c3.number_input("Consecutive losses to trigger cooldown", value=int(risk.get("consecutive_losses_trigger", 3)), min_value=1, max_value=20, step=1)

        if st.form_submit_button("Save risk settings"):
            _save_setting(session, "risk", {
                "max_position_size_pct": max_position_size_pct, "max_risk_per_trade_pct": max_risk_per_trade_pct,
                "daily_loss_limit_pct": daily_loss_limit_pct, "max_open_positions": max_open_positions,
                "max_trades_per_day": max_trades_per_day, "min_reward_to_cost_multiple": min_reward_to_cost_multiple,
                "min_confidence": min_confidence, "cooldown_hours_after_losses": cooldown_hours_after_losses,
                "consecutive_losses_trigger": consecutive_losses_trigger,
            })
            st.success("Risk settings saved.")

    st.divider()

    # --- Cost model ---
    st.subheader("Fees / TDS / slippage")
    st.caption("UNVERIFIED defaults against an official CoinDCX source -- confirm your actual tier from your own CoinDCX account's Fees page.")
    costs_row, costs = _setting(session, "costs", {})
    with st.form("costs_form"):
        c1, c2 = st.columns(2)
        maker_fee_pct = c1.number_input("Maker fee %", value=float(costs.get("maker_fee_pct", 0.2)), min_value=0.0, max_value=5.0, format="%.3f")
        taker_fee_pct = c1.number_input("Taker fee %", value=float(costs.get("taker_fee_pct", 0.2)), min_value=0.0, max_value=5.0, format="%.3f")
        tds_pct = c2.number_input("TDS %", value=float(costs.get("tds_pct", 1.0)), min_value=0.0, max_value=5.0, format="%.3f")
        slippage_pct = c2.number_input("Assumed slippage %", value=float(costs.get("slippage_pct", 0.1)), min_value=0.0, max_value=5.0, format="%.3f")
        gst_pct = st.number_input(
            "GST % (charged on the trading fee itself, not on notional)",
            value=float(costs.get("gst_pct", 18.0)), min_value=0.0, max_value=30.0, format="%.2f",
        )

        if st.form_submit_button("Save cost settings"):
            _save_setting(session, "costs", {
                "maker_fee_pct": maker_fee_pct, "taker_fee_pct": taker_fee_pct,
                "tds_pct": tds_pct, "slippage_pct": slippage_pct, "gst_pct": gst_pct,
            })
            st.success("Cost settings saved.")

    st.divider()

    # --- Running cost ---
    st.subheader("Running cost (rent)")
    st.caption("Charged once per day (IST) to every ALIVE wallet and subtracted from its equity -- so doing nothing "
               "slowly kills it. A ledger, never deducted from cash (live cash mirrors the real exchange). "
               "Benchmarks pay the same. See core/running_cost.py.")
    _, running_cost = _setting(session, "running_cost", DEFAULT_RUNNING_COST)
    with st.form("running_cost_form"):
        rc1, rc2 = st.columns(2)
        paper_daily = rc1.number_input("Paper wallets, ₹/day", value=float(running_cost.get("paper_daily_inr", 50.0)), min_value=0.0)
        live_daily = rc2.number_input("Live wallets, ₹/day", value=float(running_cost.get("live_daily_inr", 5.0)), min_value=0.0)
        if st.form_submit_button("Save running cost"):
            _save_setting(session, "running_cost", {"paper_daily_inr": paper_daily, "live_daily_inr": live_daily})
            st.success("Running cost saved -- applies from the next daily charge.")

    st.divider()

    # --- Watchlist ---
    st.subheader("Fallback watchlist")
    st.caption("Paper wallets pick their own coins every tick: top INR pairs by 24h volume, skipping stablecoins, "
               "wide spreads and 24h pumps (core/universe.py). This list is used only if CoinDCX's ticker is down. "
               "Live wallets still trade only `watchlist_live`.")
    _, watchlist = _setting(session, "watchlist", [])
    with st.form("watchlist_form"):
        watchlist_text = st.text_input("Comma-separated INR spot symbols (e.g. BTCINR,ETHINR,SOLINR)", value=",".join(watchlist))
        if st.form_submit_button("Save watchlist"):
            new_watchlist = [s.strip().upper() for s in watchlist_text.split(",") if s.strip()]
            _save_setting(session, "watchlist", new_watchlist)
            st.success("Watchlist saved.")

    st.divider()

    # --- LLM provider ---
    st.subheader("LLM provider")
    _, llm_settings = _setting(session, "llm", {})
    with st.form("llm_form"):
        provider = st.selectbox("Provider", ["nvidia", "anthropic", "openai"],
                                 index=["nvidia", "anthropic", "openai"].index(llm_settings.get("provider", "nvidia")))
        model = st.text_input("Model", value=llm_settings.get("model", ""))
        if st.form_submit_button("Save LLM settings"):
            _save_setting(session, "llm", {"provider": provider, "model": model})
            st.success("LLM settings saved -- the matching API key must already be set in the worker's environment.")

    st.divider()

    # --- Telegram alert toggles ---
    st.subheader("Telegram alerts")
    _, telegram_alerts = _setting(session, "telegram_alerts", {})
    alert_types = [
        "trade_executed", "strategy_ready_for_live", "daily_loss_limit_hit", "cooldown_triggered",
        "heartbeat_missing", "repeated_llm_failures", "agent_died", "daily_summary",
    ]
    with st.form("telegram_form"):
        new_toggles = {}
        cols = st.columns(2)
        for i, alert_type in enumerate(alert_types):
            new_toggles[alert_type] = cols[i % 2].checkbox(alert_type.replace("_", " "), value=telegram_alerts.get(alert_type, True))
        if st.form_submit_button("Save Telegram settings"):
            _save_setting(session, "telegram_alerts", new_toggles)
            st.success("Telegram alert settings saved.")

    st.divider()

    # --- Death threshold ---
    st.subheader("Death threshold")
    _, death_threshold_pct = _setting(session, "death_threshold_pct", 20.0)
    with st.form("death_threshold_form"):
        new_threshold = st.number_input(
            "Equity below this %% of starting capital kills a wallet permanently",
            value=float(death_threshold_pct), min_value=1.0, max_value=99.0,
        )
        if st.form_submit_button("Save death threshold"):
            _save_setting(session, "death_threshold_pct", new_threshold)
            st.success("Death threshold saved.")
