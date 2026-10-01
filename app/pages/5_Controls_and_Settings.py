import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

# st.set_page_config() must be the very first Streamlit command -- see Home.py's comment
st.set_page_config(page_title="Controls & Settings - Survivor", page_icon="⚙️", layout="wide")

from app.lib.bootstrap import ensure_env_from_secrets

ensure_env_from_secrets()

from app.lib.auth import current_email, require_login, sign_out
from app.lib.queries import list_wallets
from core.db.models import ControlCommand, Setting
from core.db.session import get_session

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


with get_session() as session:
    # --- Global kill switch ---
    st.subheader("Kill switch")
    global_row, global_settings = _setting(session, "global", {"kill_switch": False, "mode": "paper", "require_human_approval_for_live": True})
    kill_switch = st.toggle("Kill switch (blocks every new entry, every wallet)", value=global_settings.get("kill_switch", False))
    if kill_switch != global_settings.get("kill_switch", False):
        _save_setting(session, "global", {**global_settings, "kill_switch": kill_switch})
        st.success("Kill switch updated.")
        st.rerun()
    st.caption(f"Mode: **{global_settings.get('mode', 'paper')}** -- live trading doesn't exist yet (v1 scope).")

    st.divider()

    # --- Per-wallet pause/resume/reset ---
    st.subheader("Wallets")
    for wallet in list_wallets(session):
        agent = wallet.agent
        cols = st.columns([2, 2, 2, 2, 3])
        cols[0].write(f"**{wallet.name}**")
        cols[1].write(f"status: `{agent.status if agent else 'unknown'}`")

        if agent and agent.status == "alive":
            if cols[2].button("Pause", key=f"pause_{wallet.id}"):
                agent.status = "paused"
                st.rerun()
        elif agent and agent.status == "paused":
            if cols[2].button("Resume", key=f"resume_{wallet.id}"):
                agent.status = "alive"
                st.rerun()
        else:
            cols[2].caption("dead -- reset to revive")

        with cols[3].popover("Reset..."):
            st.warning(f"This wipes ALL of {wallet.name}'s trades/positions/decisions/equity history and restores starting capital. Cannot be undone.")
            typed = st.text_input("Type the wallet name to confirm", key=f"reset_confirm_{wallet.id}")
            if st.button("Confirm reset", key=f"reset_btn_{wallet.id}", disabled=(typed != wallet.name)):
                session.add(ControlCommand(id=uuid.uuid4(), wallet_id=wallet.id, command="reset_wallet", payload={}, status="pending"))
                st.success("Reset queued -- the worker applies it on its next tick.")
                st.rerun()

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

        if st.form_submit_button("Save cost settings"):
            _save_setting(session, "costs", {
                "maker_fee_pct": maker_fee_pct, "taker_fee_pct": taker_fee_pct,
                "tds_pct": tds_pct, "slippage_pct": slippage_pct,
            })
            st.success("Cost settings saved.")

    st.divider()

    # --- Watchlist ---
    st.subheader("Watchlist")
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
