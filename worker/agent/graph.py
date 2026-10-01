"""Compiled with NO checkpointer. The worker is stateless between
ticks by design (spec section 3 -- every value is loaded from the DB
at the start of a tick and persisted before returning), and v1 has no
human-in-the-loop interrupt that needs to pause and resume a graph run
across requests. A `.invoke()` call runs start-to-finish synchronously
within one HTTP request, so there's no partial state to persist.
`langchain-cockroachdb`'s `CockroachDBSaver` is worth revisiting only
if a future phase adds an interrupt (e.g. a dashboard approval step
mid-cycle) -- don't add it speculatively before that's real.
"""

from __future__ import annotations

from langgraph.graph import END, StateGraph

from .nodes import check_exits, decide, generate_mechanical_signals, load_market, risk_and_execute, strategize
from .state import AgentState


def build_graph():
    graph = StateGraph(AgentState)
    graph.add_node("load_market", load_market)
    graph.add_node("check_exits", check_exits)
    graph.add_node("strategize", strategize)
    graph.add_node("generate_mechanical_signals", generate_mechanical_signals)
    graph.add_node("decide", decide)
    graph.add_node("risk_and_execute", risk_and_execute)

    graph.set_entry_point("load_market")
    graph.add_edge("load_market", "check_exits")
    graph.add_edge("check_exits", "strategize")
    graph.add_edge("strategize", "generate_mechanical_signals")
    graph.add_edge("generate_mechanical_signals", "decide")
    graph.add_edge("decide", "risk_and_execute")
    graph.add_edge("risk_and_execute", END)
    return graph.compile()
