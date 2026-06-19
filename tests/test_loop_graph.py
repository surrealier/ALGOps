"""The full LangGraph loop with the toy executor (deterministic search behavior)."""

from __future__ import annotations

import os
import tempfile

import pytest
from langgraph.checkpoint.memory import MemorySaver

from algops.agents import (
    LocalRunExecutor,
    LoopDeps,
    NullHooks,
    build_graph,
    initial_state,
    recursion_limit_for,
)
from algops.core import RunRequest, new_id
from algops.toy_project import load_param_spec, run as toy_run

WORK = tempfile.mkdtemp(prefix="algops_test_loop_")
PK = "detector.confidence_threshold"


def _local_run(req: RunRequest):
    return toy_run(req, os.path.join(WORK, req.run_id))


def _build():
    deps = LoopDeps(executor=LocalRunExecutor(_local_run), hooks=NullHooks())
    return build_graph(deps, checkpointer=MemorySaver())


def _state(**kw):
    base = dict(
        experiment_id="exp", project_id="p", project_name="toy", goal_text="오탐 완화",
        target_metric="fp_rate", target_value=0.05, goal_direction="minimize", max_iteration=10,
        test_set_uri="bundled://toy", primary_param_key=PK, param_spec=load_param_spec().model_dump(),
    )
    base.update(kw)
    return initial_state(**base)


async def test_loop_reaches_target():
    out = await _build().ainvoke(
        _state(),
        {"configurable": {"thread_id": new_id()}, "recursion_limit": recursion_limit_for(10)},
    )
    assert out["stop_reason"] == "target_reached"
    assert out["final_metrics"]["fp_rate"] <= 0.05
    thresholds = [h["params"][PK] for h in out["history"]]
    assert thresholds == sorted(thresholds)  # monotonic climb for fp_rate minimization


async def test_loop_respects_max_iteration():
    # Unreachable target (fp_rate <= 0.0) -> must stop at the cap, enforced by the router.
    out = await _build().ainvoke(
        _state(target_value=0.0, max_iteration=3),
        {"configurable": {"thread_id": new_id()}, "recursion_limit": recursion_limit_for(3)},
    )
    assert out["iteration"] == 3
    assert out["stop_reason"] == "max_iteration"


async def test_loop_maximize_recall():
    out = await _build().ainvoke(
        _state(target_metric="recall", target_value=0.97, goal_direction="maximize"),
        {"configurable": {"thread_id": new_id()}, "recursion_limit": recursion_limit_for(10)},
    )
    # Maximizing recall drives the threshold DOWN (opposite of fp_rate minimization).
    assert out["stop_reason"] == "target_reached"
    assert out["final_metrics"]["recall"] >= 0.97
    thresholds = [h["params"][PK] for h in out["history"]]
    assert thresholds == sorted(thresholds, reverse=True)
