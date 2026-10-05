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
st.caption(
    "A call counts as **answered** if the main model replied, or it failed and the backup model "
    "(core/llm/provider.py fallback) replied instead. Only unanswered calls cost a decision -- the agent "
    "holds on those. Rows logged before 2026-10-05 predate rescue tracking and may show as unanswered."
)

with get_session() as session:
    calls = session.query(LLMCall).order_by(LLMCall.created_at.desc()).limit(1000).all()

    if not calls:
        st.info("No LLM calls logged yet.")
        st.stop()

    first_attempts = [c for c in calls if not c.is_fallback]  # one per logical call
    fallbacks = [c for c in calls if c.is_fallback]

    for node in sorted({c.node for c in first_attempts}):
        node_calls = [c for c in first_attempts if c.node == node]
        answered = [c for c in node_calls if c.success or c.rescued_by]
        rescued = [c for c in node_calls if c.rescued_by]
        ok = [c for c in node_calls if c.success] + [c for c in fallbacks if c.node == node and c.success]
        st.subheader(f"`{node}` node")
        cols = st.columns(5)
        cols[0].metric("Answered", f"{len(answered) / len(node_calls) * 100:.1f}%", help="main model or backup replied")
        cols[1].metric("Calls (last 1000 rows)", len(node_calls))
        cols[2].metric("Main model OK", f"{sum(c.success for c in node_calls) / len(node_calls) * 100:.0f}%")
        cols[3].metric("Rescued by backup", len(rescued))
        cols[4].metric("Avg latency (answered)", f"{sum(c.latency_ms for c in ok) / len(ok) / 1000:.1f} s" if ok else "-")
        st.caption(f"Main model(s): {', '.join(sorted({c.model for c in node_calls}))}"
                   + (f" · backup: {', '.join(sorted({c.model for c in fallbacks if c.node == node}))}"
                      if any(c.node == node for c in fallbacks) else ""))

    unanswered = [c for c in first_attempts if not c.success and not c.rescued_by]
    st.subheader("Unanswered calls (agent held)")
    if unanswered:
        st.dataframe([
            {"Time": c.created_at, "Node": c.node, "Model": c.model, "Error": c.error} for c in unanswered[:50]
        ], use_container_width=True)
    else:
        st.caption("None -- every call got an answer.")

    rescued_rows = [c for c in first_attempts if c.rescued_by]
    with st.expander(f"Rescued by backup ({len(rescued_rows)}) -- main model failed, backup answered"):
        st.dataframe([
            {"Time": c.created_at, "Node": c.node, "Main model": c.model, "Answered by": c.rescued_by, "Main model error": c.error}
            for c in rescued_rows[:100]
        ], use_container_width=True)
