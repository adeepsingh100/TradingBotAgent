import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

# st.set_page_config() must be the very first Streamlit command -- see Home.py's comment
st.set_page_config(page_title="Trades - Survivor", page_icon="\U0001f4ca", layout="wide")

from app.lib.bootstrap import ensure_env_from_secrets

ensure_env_from_secrets()

from app.lib.queries import list_wallets
from core.db.models import Trade
from core.db.session import get_session
st.title("Trades")

with get_session() as session:
    wallets = list_wallets(session)
    if not wallets:
        st.info("No wallets yet.")
        st.stop()

    wallet_name = st.selectbox("Wallet", [w.name for w in wallets])
    wallet = next(w for w in wallets if w.name == wallet_name)

    trades = (
        session.query(Trade)
        .filter_by(wallet_id=wallet.id)
        .order_by(Trade.executed_at.desc())
        .limit(500)
        .all()
    )

    if not trades:
        st.caption("No trades yet.")
        st.stop()

    closed = [t for t in trades if t.pnl is not None]
    wins = [t for t in closed if t.pnl > 0]
    cols = st.columns(3)
    cols[0].metric("Closed trades", len(closed))
    cols[1].metric("Win rate", f"{len(wins) / len(closed) * 100:.0f}%" if closed else "--")
    cols[2].metric("Total realized PnL", f"₹{sum(t.pnl for t in closed):,.2f}" if closed else "₹0.00")

    st.dataframe([
        {
            "Time": t.executed_at, "Pair": t.pair, "Side": t.side, "Qty": t.qty, "Price": t.price,
            "Fee": t.fee, "TDS": t.tds, "Slippage": t.slippage, "PnL": t.pnl,
        }
        for t in trades
    ], use_container_width=True)
