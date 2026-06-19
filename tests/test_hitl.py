"""M5: human-in-the-loop approval gate — the Developer pauses, resumes on approval."""

from __future__ import annotations

import os
import tempfile

from langgraph.checkpoint.memory import MemorySaver
from langgraph.types import Command

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

WORK = tempfile.mkdtemp(prefix="algops_test_hitl_")
PK = "detector.confidence_threshold"


def _local_run(req: RunRequest):
    return toy_run(req, os.path.join(WORK, req.run_id))


async def test_interrupt_then_resume_completes():
    deps = LoopDeps(executor=LocalRunExecutor(_local_run), hooks=NullHooks())
    graph = build_graph(deps, checkpointer=MemorySaver())
    state = initial_state(
        experiment_id="e", project_id="p", project_name="toy", goal_text="오탐 완화",
        target_metric="fp_rate", target_value=0.05, goal_direction="minimize", max_iteration=3,
        test_set_uri="bundled://toy", primary_param_key=PK, param_spec=load_param_spec().model_dump(),
        require_approval=True,
    )
    cfg = {"configurable": {"thread_id": new_id()}, "recursion_limit": recursion_limit_for(3)}

    out = await graph.ainvoke(state, cfg)
    assert out.get("__interrupt__"), "Developer should pause for approval"
    payload = out["__interrupt__"][0].value
    assert payload["type"] == "approval_required" and PK in payload["proposed_params"]

    pauses = 0
    while out.get("__interrupt__") and pauses < 10:
        pauses += 1
        out = await graph.ainvoke(Command(resume=True), cfg)  # human approves

    assert pauses >= 1
    assert out.get("stop_reason") in {"target_reached", "max_iteration", "converged", "pm_stop"}
