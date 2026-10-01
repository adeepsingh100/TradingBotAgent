import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

import streamlit as st

# st.set_page_config() must be the very first Streamlit command -- see Home.py's comment
st.set_page_config(page_title="Strategies - Survivor", page_icon="\U0001f9ea", layout="wide")

from app.lib.bootstrap import ensure_env_from_secrets

ensure_env_from_secrets()

from core.db.models import Backtest, Setting, Strategy
from core.db.session import get_session
from core.promotion import clears_live_promotion, gather_live_promotion_stats

st.title("Strategies")
st.caption(
    "The global strategy library -- not wallet-scoped (see CLAUDE.md). Read-only here; "
    "promoting a strategy to live-eligible is a separate, auth-gated action on Controls & Settings."
)

with get_session() as session:
    strategies = session.query(Strategy).order_by(Strategy.type).all()
    backtests = session.query(Backtest).order_by(Backtest.created_at.desc()).all()
    promotion_settings = session.query(Setting).filter_by(key="promotion").one_or_none()
    live_criteria = (promotion_settings.value if promotion_settings else {}).get("live_criteria", {
        "min_trades": 20, "min_days": 14, "min_profit_factor": 1.2, "max_drawdown_pct": 15,
    })

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

    st.subheader("Live-promotion status")
    st.caption(
        "Aggregated paper track record per strategy (every paper wallet that's traded it) against spec section 5's "
        "strict bar. Clearing this is necessary but never sufficient -- promoting still requires a human click on "
        "Controls & Settings (settings.global.require_human_approval_for_live)."
    )
    for strategy in strategies:
        stats = gather_live_promotion_stats(session, strategy.id)
        if stats is None:
            st.caption(f"**{strategy.type}**: no closed paper trades yet.")
            continue
        benchmark_return_pct = stats.pop("benchmark_return_pct")
        passed, reasons = clears_live_promotion(stats, live_criteria, benchmark_return_pct)
        emoji = "✅" if passed else "❌"
        label = (
            f"{emoji} **{strategy.type}** ({strategy.status}) -- {stats['trade_count']} trades, "
            f"{stats['days_tested']:.1f} days, return {stats['return_pct']:.2f}% vs. BTC {benchmark_return_pct:.2f}%"
        )
        with st.expander(label):
            st.json(stats)
            if reasons:
                for reason in reasons:
                    st.caption(f"- {reason}")
            else:
                st.caption("Clears every criterion.")
