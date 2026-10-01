import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # repo root -- see app/lib/bootstrap.py's docstring

import streamlit as st

# st.set_page_config() must be the very first Streamlit command on a page --
# even st.secrets (inside ensure_env_from_secrets) counts as "a command" if
# called first, so this has to come before that import/call.
st.set_page_config(page_title="Survivor", page_icon="🤖", layout="wide")

from app.lib.bootstrap import ensure_env_from_secrets

ensure_env_from_secrets()

from datetime import datetime, timezone

import plotly.graph_objects as go
from streamlit_autorefresh import st_autorefresh

from app.lib.queries import list_wallets
from core.coindcx.client import get_ticker
from core.db.models import EquityHistory, Heartbeat, Position
from core.db.session import get_session
st_autorefresh(interval=60_000, key="home_autorefresh")


@st.cache_data(ttl=60)  # one public ticker call per minute, same cadence as the autorefresh
def _live_prices() -> dict[str, float]:
    try:
        return {r["market"]: float(r["last_price"]) for r in get_ticker() if r.get("last_price")}
    except Exception:  # noqa: BLE001 -- a ticker outage blanks the price columns, never breaks the page
        return {}


st.title("Survivor")

with get_session() as session:
    wallets = list_wallets(session)
    if not wallets:
        st.info("No wallets yet -- run `python -m scripts.seed_wallets`.")
        st.stop()

    wallet_name = st.selectbox("Wallet", [w.name for w in wallets])
    wallet = next(w for w in wallets if w.name == wallet_name)

    open_positions = session.query(Position).filter_by(wallet_id=wallet.id, closed_at=None).all()
    history = (
        session.query(EquityHistory)
        .filter_by(wallet_id=wallet.id)
        .order_by(EquityHistory.recorded_at)
        .all()
    )
    heartbeat = session.query(Heartbeat).filter_by(source="worker_tick").one_or_none()

    agent_points = [h for h in history if h.series == "agent"]
    current_equity = agent_points[-1].equity_inr if agent_points else wallet.current_cash

    status = wallet.agent.status if wallet.agent else "unknown"
    cols = st.columns(5)
    cols[0].metric("Status", status)
    cols[1].metric("Equity", f"₹{current_equity:,.2f}")
    cols[2].metric("Cash", f"₹{wallet.current_cash:,.2f}")
    cols[3].metric("TDS credit", f"₹{wallet.tds_credit:,.2f}")
    cols[4].metric("Starting capital", f"₹{wallet.starting_capital:,.2f}")

    if heartbeat is not None:
        age_seconds = (datetime.now(timezone.utc) - heartbeat.last_beat_at).total_seconds()
        if age_seconds > 30 * 60:
            st.warning(f"Worker heartbeat is {age_seconds / 60:.0f} minutes old -- the worker may be stuck.")
    else:
        st.warning("No worker heartbeat recorded yet.")

    st.subheader("Equity vs. benchmarks")
    fig = go.Figure()
    series_labels = {"agent": "Agent", "benchmark_btc": "Buy & hold BTC", "benchmark_cash": "Hold cash"}
    for series, label in series_labels.items():
        points = [h for h in history if h.series == series]
        if points:
            fig.add_trace(go.Scatter(
                x=[p.recorded_at for p in points], y=[p.equity_inr for p in points],
                mode="lines", name=label,
            ))
    fig.update_layout(xaxis_title="Time", yaxis_title="Equity (INR)", height=450)
    st.plotly_chart(fig, use_container_width=True)

    st.subheader("Open positions")
    if open_positions:
        prices = _live_prices()
        if not prices:
            st.caption("Live prices unavailable right now (CoinDCX ticker unreachable).")
        rows = []
        for p in open_positions:
            price = prices.get(p.pair)
            cost = p.qty * p.entry_price
            value = p.qty * price if price is not None else None
            rows.append({
                "Pair": p.pair, "Qty": p.qty, "Entry": p.entry_price, "Current price": price,
                "Cost (₹)": round(cost, 2), "Value now (₹)": round(value, 2) if value is not None else None,
                "P&L (₹)": round(value - cost, 2) if value is not None else None,
                "P&L %": round((value - cost) / cost * 100, 2) if value is not None and cost else None,
                "Stop loss": p.stop_loss, "Take profit": p.take_profit, "Opened at": p.opened_at,
            })
        st.dataframe(rows, use_container_width=True)
        st.caption("Value now = qty × live price, before exit fee, TDS and slippage.")
    else:
        st.caption("No open positions.")
