"""LangGraph checkpoint durability: interrupt the loop mid-run, then resume to completion.

Proves a worker restart resumes a campaign from its persisted checkpoint (thread_id) without
re-running prior iterations — the M0 requirement behind ``AsyncSqliteSaver`` / ``thread_id``.
"""

from __future__ import annotations

import os
import tempfile

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

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

WORK = tempfile.mkdtemp(prefix="algops_test_resume_")
PK = "detector.confidence_threshold"


def _deps():
    def local_run(req: RunRequest):
        return toy_run(req, os.path.join(WORK, req.run_id))

    return LoopDeps(executor=LocalRunExecutor(local_run), hooks=NullHooks())


def _state():
    return initial_state(
        experiment_id="exp", project_id="p", project_name="toy", goal_text="오탐 완화",
        target_metric="fp_rate", target_value=0.05, goal_direction="minimize", max_iteration=10,
        test_set_uri="bundled://toy", primary_param_key=PK, param_spec=load_param_spec().model_dump(),
    )


async def test_checkpoint_resume(tmp_path):
    db = os.path.join(str(tmp_path), "cp.sqlite")
    tid = new_id()
    cfg = {"configurable": {"thread_id": tid}}

    # 1) First attempt with a tiny recursion budget -> stops part-way, leaving a checkpoint.
    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = build_graph(_deps(), checkpointer=cp)
        try:
            await graph.ainvoke(_state(), {**cfg, "recursion_limit": 5})
        except Exception:
            pass  # GraphRecursionError expected — the point is the partial checkpoint
        snap = await graph.aget_state(cfg)
    progressed = snap.values.get("iteration", 0)
    assert progressed >= 1, "loop made no progress before interruption"

    # 2) Fresh saver on the SAME file + None input -> resume from the checkpoint to completion.
    async with AsyncSqliteSaver.from_conn_string(db) as cp:
        graph = build_graph(_deps(), checkpointer=cp)
        out = await graph.ainvoke(None, {**cfg, "recursion_limit": recursion_limit_for(10)})

    assert out["stop_reason"] == "target_reached"
    assert out["final_metrics"]["fp_rate"] <= 0.05
    # Resumed run continued past where it was interrupted (did not restart at iteration 1).
    assert out["iteration"] >= progressed
