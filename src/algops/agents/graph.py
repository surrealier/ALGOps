"""Assemble the experiment-loop StateGraph (plan §4).

Linear chain + one conditional edge out of ``pm_decide`` (continue | validate → finalize). ``pm_decide`` is the *only* node
with conditional edges; every other node uses plain edges (never mix the two on one node).
"""

from __future__ import annotations

from langgraph.graph import END, START, StateGraph

from .nodes import LoopDeps, make_nodes, route_after_pm
from .state import AlgOpsState

_CHAIN = [
    ("rag_retrieve", "pm_propose"),
    ("pm_propose", "developer"),
    ("developer", "run_eval"),
    ("run_eval", "extract_csv"),
    ("extract_csv", "pandas_summarize"),
    ("pandas_summarize", "analyst"),
    ("analyst", "pm_decide"),
]


def build_graph(deps: LoopDeps, checkpointer=None):
    nodes = make_nodes(deps)
    graph = StateGraph(AlgOpsState)
    for name, fn in nodes.items():
        graph.add_node(name, fn)
    graph.add_edge(START, "rag_retrieve")
    for src, dst in _CHAIN:
        graph.add_edge(src, dst)
    graph.add_conditional_edges(
        "pm_decide",
        route_after_pm,
        {"continue": "rag_retrieve", "finalize": "validate"},
    )
    graph.add_edge("validate", "finalize")
    graph.add_edge("finalize", END)
    return graph.compile(checkpointer=checkpointer)


def recursion_limit_for(max_iteration: int) -> int:
    """A crash-guard only — real termination is the router's max_iteration/target checks."""
    return max_iteration * 8 + 10
