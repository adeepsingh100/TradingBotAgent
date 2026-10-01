import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

# st.set_page_config() must be the very first Streamlit command -- see Home.py's comment
st.set_page_config(page_title="Model Health - Survivor", page_icon="\U0001f916", layout="wide")

from app.lib.bootstrap import ensure_env_from_secrets

ensure_env_from_secrets()

from core.db.models import LLMCall
from core.db.session import get_session
st.title("Model Health")

with get_session() as session:
    calls = session.query(LLMCall).order_by(LLMCall.created_at.desc()).limit(500).all()

    if not calls:
        st.info("No LLM calls logged yet.")
        st.stop()

    for node in sorted({c.node for c in calls}):
        node_calls = [c for c in calls if c.node == node]
        successes = [c for c in node_calls if c.success]
        st.subheader(f"`{node}` node")
        cols = st.columns(3)
        cols[0].metric("Calls (last 500)", len(node_calls))
        cols[1].metric("Success rate", f"{len(successes) / len(node_calls) * 100:.0f}%")
        cols[2].metric("Avg latency", f"{sum(c.latency_ms for c in node_calls) / len(node_calls):.0f} ms")

    failures = [c for c in calls if not c.success]
    st.subheader("Recent failures")
    if failures:
        st.dataframe([
            {"Time": c.created_at, "Node": c.node, "Provider": c.provider, "Model": c.model, "Error": c.error}
            for c in failures[:50]
        ], use_container_width=True)
    else:
        st.caption("No failures in the last 500 calls.")
