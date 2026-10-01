import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

# st.set_page_config() must be the very first Streamlit command -- see Home.py's comment
st.set_page_config(page_title="Strategies - Survivor", page_icon="\U0001f9ea", layout="wide")

from app.lib.bootstrap import ensure_env_from_secrets

ensure_env_from_secrets()

from core.db.models import Backtest, Strategy
from core.db.session import get_session
st.title("Strategies")
st.caption("The global strategy library -- not wallet-scoped (see CLAUDE.md). Read-only: tuning/promotion happens via the agent and `scripts/`, not from here.")

with get_session() as session:
    strategies = session.query(Strategy).order_by(Strategy.type).all()
    backtests = session.query(Backtest).order_by(Backtest.created_at.desc()).all()

    if not strategies:
        st.info("No strategies seeded yet -- run `python -m scripts.seed_strategies`.")
        st.stop()

    st.dataframe([
        {"Type": s.type, "Status": s.status, "Params": s.params, "Stats": s.stats, "Updated": s.updated_at}
        for s in strategies
    ], use_container_width=True)

    st.subheader("Backtest history")
    strategy_type_by_id = {s.id: s.type for s in strategies}
    if backtests:
        st.dataframe([
            {
                "Strategy": strategy_type_by_id.get(b.strategy_id, "?"),
                "Trades": b.trade_count, "Net PnL": b.net_pnl, "Win rate": b.win_rate,
                "Profit factor": b.profit_factor, "Max drawdown %": b.max_drawdown_pct,
                "Period": f"{b.start_date:%Y-%m-%d} to {b.end_date:%Y-%m-%d}", "Run at": b.created_at,
            }
            for b in backtests
        ], use_container_width=True)
    else:
        st.caption("No backtests recorded yet.")
