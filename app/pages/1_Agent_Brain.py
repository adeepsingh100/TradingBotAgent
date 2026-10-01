import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

# st.set_page_config() must be the very first Streamlit command -- see Home.py's comment
st.set_page_config(page_title="Agent Brain - Survivor", page_icon="\U0001f9e0", layout="wide")

from app.lib.bootstrap import ensure_env_from_secrets

ensure_env_from_secrets()

from app.lib.queries import list_wallets
from core.db.models import Decision
from core.db.session import get_session
st.title("Agent Brain")
st.caption("Every decision the agent made, regardless of outcome -- a hold or a risk-manager rejection still shows up here.")

with get_session() as session:
    wallets = list_wallets(session)
    if not wallets:
        st.info("No wallets yet.")
        st.stop()

    wallet_name = st.selectbox("Wallet", [w.name for w in wallets])
    wallet = next(w for w in wallets if w.name == wallet_name)

    decisions = (
        session.query(Decision)
        .filter_by(wallet_id=wallet.id)
        .order_by(Decision.created_at.desc())
        .limit(200)
        .all()
    )

    if not decisions:
        st.caption("No decisions recorded yet.")
    for decision in decisions:
        proposal = decision.proposal or {}
        verdict_emoji = {"approved": "✅", "downsized": "⚠️", "rejected": "❌", "hold": "⏸️"}
        label = f"{verdict_emoji.get(decision.risk_verdict, '')} {decision.created_at:%Y-%m-%d %H:%M:%S} -- {proposal.get('pair', '?')} -- {decision.risk_verdict}"
        with st.expander(label):
            st.json(proposal)
            st.write(f"**Risk verdict:** {decision.risk_verdict} -- {decision.risk_reason}")
            if decision.executed_trade_id:
                st.write(f"**Executed trade:** `{decision.executed_trade_id}`")
