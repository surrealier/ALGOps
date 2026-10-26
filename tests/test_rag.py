"""M3: in-memory RAG retrieval + learned parameter influence + cross-experiment memory."""

from __future__ import annotations

import os
import tempfile

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
from algops.rag import InMemoryRagStore, RagQuery, RunReport
from algops.toy_project import load_param_spec, run as toy_run

PK = "detector.confidence_threshold"
WORK = tempfile.mkdtemp(prefix="algops_test_rag_")


def test_inmemory_retrieval_and_param_influence():
    store = InMemoryRagStore()
    store.index_run(RunReport(run_id="A", experiment_id="e1", project_id="p", iteration_index=1,
                              goal_tag="오탐 완화", params={PK: 0.45}, params_changed=[],
                              primary_metric="fp_rate", primary_metric_value=0.279, report_md="기준 실험"))
    store.index_run(RunReport(run_id="B", experiment_id="e1", project_id="p", iteration_index=2,
                              goal_tag="오탐 완화", params={PK: 0.50}, params_changed=[PK],
                              metric_deltas={"fp_rate": -0.08}, primary_metric="fp_rate",
                              primary_metric_value=0.193, report_md="threshold 상향", derived_from="A"))
    ctx = store.retrieve(RagQuery(project_id="p", goal_text="오탐 완화", target_metric="fp_rate", top_k=5))
    assert ctx.anchor_run_ids and set(ctx.anchor_run_ids) <= {"A", "B"}
    assert "과거 유사 실험" in ctx.text
    hint = next((h for h in ctx.param_hints if h.param_key == PK), None)
    assert hint is not None and hint.direction == "up" and hint.helped >= 1  # raising threshold helped fp_rate

    # Different project is isolated.
    assert store.retrieve(RagQuery(project_id="other", goal_text="x", target_metric="fp_rate")).text == ""


class _RagHooks(NullHooks):
    """Wire the loop's RAG hooks to an InMemoryRagStore (no DB), for testing memory."""

    def __init__(self, store: InMemoryRagStore) -> None:
        self.store = store

    def index_run_report(self, **fields):
        self.store.index_run(RunReport(**fields))

    def retrieve_rag(self, *, project_id, goal_text, target_metric, top_k, algorithm_id=None):
        ctx = self.store.retrieve(
            RagQuery(project_id=project_id, goal_text=goal_text, target_metric=target_metric, top_k=top_k)
        )
        return {"text": ctx.text, "anchor_ids": ctx.anchor_run_ids}


def _local_run(req: RunRequest):
    return toy_run(req, os.path.join(WORK, req.run_id))


def _run_experiment(store, project_id, eid):
    deps = LoopDeps(executor=LocalRunExecutor(_local_run), hooks=_RagHooks(store))
    graph = build_graph(deps, checkpointer=MemorySaver())
    state = initial_state(
        experiment_id=eid, project_id=project_id, project_name="toy", goal_text="오탐 완화",
        target_metric="fp_rate", target_value=0.05, goal_direction="minimize", max_iteration=10,
        test_set_uri="bundled://toy", primary_param_key=PK, param_spec=load_param_spec().model_dump(),
    )
    return graph.ainvoke(
        state, {"configurable": {"thread_id": new_id()}, "recursion_limit": recursion_limit_for(10)}
    )


async def test_cross_experiment_memory():
    store = InMemoryRagStore()
    await _run_experiment(store, "proj-X", "exp-1")
    indexed_after_first = store.num_docs()
    assert indexed_after_first >= 1  # every run report was indexed

    out2 = await _run_experiment(store, "proj-X", "exp-2")
    # exp-2 retrieved prior context (from exp-1 and its own runs) — memory crosses experiments.
    assert out2["rag_context"]
    assert "과거 유사 실험" in out2["rag_context"]
    assert store.num_docs() > indexed_after_first
