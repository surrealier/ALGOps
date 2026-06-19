"""algops.agents — the LangGraph experiment loop (M0: deterministic stubs)."""

from __future__ import annotations

from .executor import LocalRunExecutor, RunExecutor
from .graph import build_graph, recursion_limit_for
from .hooks import LoopHooks, NullHooks
from .nodes import LoopDeps, make_nodes, route_after_pm
from .state import AlgOpsState, initial_state

__all__ = [
    "LocalRunExecutor",
    "RunExecutor",
    "build_graph",
    "recursion_limit_for",
    "LoopHooks",
    "NullHooks",
    "LoopDeps",
    "make_nodes",
    "route_after_pm",
    "AlgOpsState",
    "initial_state",
]
