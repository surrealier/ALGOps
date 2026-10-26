"""M1: the LLM planner integration + the loop's LLM-backed branch (no server, fake models)."""

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
from algops.llm.planner import Decision, OpenAILLMPlanner, ParamProposal
from algops.toy_project import load_param_spec, run as toy_run

WORK = tempfile.mkdtemp(prefix="algops_test_llm_")
PK = "detector.confidence_threshold"


# --- fakes ---------------------------------------------------------------------------------


class _FakeStructured:
    def __init__(self, obj):
        self._obj = obj

    async def ainvoke(self, _messages):
        return self._obj


class _FakeChat:
    """A chat model that honors with_structured_output / ainvoke with canned outputs."""

    def __init__(self, proposal, decision, text="분석 텍스트"):
        self._proposal = proposal
        self._decision = decision
        self._text = text

    def with_structured_output(self, schema, method=None):
        return _FakeStructured(self._proposal if schema is ParamProposal else self._decision)

    async def ainvoke(self, _messages):
        class _Msg:
            content = self._text

        return _Msg()


class _ClimbPlanner:
    """A planner that drives the loop's LLM branch end to end (no chat model)."""

    async def propose(self, state, spec):
        last = state["history"][-1]["params"][PK]
        return ParamProposal(param_key=PK, new_value=last + 0.05, rationale="threshold ↑")

    async def analyze(self, state):
        return "fp_rate 개선 중 (LLM 분석)"

    async def decide(self, state):
        return Decision(action="continue")

    async def summarize(self, state):
        return "# LLM 리포트"


def _local_run(req: RunRequest):
    return toy_run(req, os.path.join(WORK, req.run_id))


# --- tests ---------------------------------------------------------------------------------


async def test_openai_planner_structured_and_text():
    planner = OpenAILLMPlanner(
        _FakeChat(
            proposal=ParamProposal(param_key=PK, new_value=0.6, rationale="r"),
            decision=Decision(action="stop", stop_reason="pm_stop"),
        )
    )
    state = {
        "goal_text": "오탐 완화", "target_metric": "fp_rate", "goal_direction": "minimize",
        "target_value": 0.05, "history": [{"iteration": 1, "params": {PK: 0.45}, "objective": 0.2}],
        "rag_context": "", "current_params": {PK: 0.45}, "iteration": 1, "project_name": "toy",
        "last_metrics": {"tp": 1, "fp": 1, "fn": 1, "tn": 1, "precision": 0.5, "recall": 0.5,
                          "f1": 0.5, "fp_rate": 0.5, "fn_rate": 0.5},
        "analyst_findings": "", "csv_drop_reasons": {},
    }
    prop = await planner.propose(state, load_param_spec())
    assert isinstance(prop, ParamProposal) and prop.param_key == PK
    dec = await planner.decide(state)
    assert isinstance(dec, Decision) and dec.action == "stop"
    assert await planner.analyze(state)
    assert await planner.summarize(state)


async def test_loop_uses_llm_branch():
    deps = LoopDeps(executor=LocalRunExecutor(_local_run), hooks=NullHooks(), planner=_ClimbPlanner())
    graph = build_graph(deps, checkpointer=MemorySaver())
    state = initial_state(
        experiment_id="e", project_id="p", project_name="toy", goal_text="오탐 완화",
        target_metric="fp_rate", target_value=0.05, goal_direction="minimize", max_iteration=10,
        test_set_uri="bundled://toy", primary_param_key=PK, param_spec=load_param_spec().model_dump(),
    )
    out = await graph.ainvoke(
        state, {"configurable": {"thread_id": new_id()}, "recursion_limit": recursion_limit_for(10)}
    )
    assert out["stop_reason"] == "target_reached"
    assert out["analyst_findings"] == "fp_rate 개선 중 (LLM 분석)"  # LLM analyst path ran
    assert out["csv_summary_md"].startswith("# LLM 리포트")  # LLM summarize path ran


async def test_planner_failure_falls_back_to_deterministic():
    class _BoomPlanner:
        async def propose(self, *a):
            raise RuntimeError("llm down")

        async def analyze(self, *a):
            raise RuntimeError("llm down")

        async def decide(self, *a):
            raise RuntimeError("llm down")

        async def summarize(self, *a):
            raise RuntimeError("llm down")

    deps = LoopDeps(executor=LocalRunExecutor(_local_run), hooks=NullHooks(), planner=_BoomPlanner())
    graph = build_graph(deps, checkpointer=MemorySaver())
    state = initial_state(
        experiment_id="e", project_id="p", project_name="toy", goal_text="오탐 완화",
        target_metric="fp_rate", target_value=0.05, goal_direction="minimize", max_iteration=10,
        test_set_uri="bundled://toy", primary_param_key=PK, param_spec=load_param_spec().model_dump(),
    )
    out = await graph.ainvoke(
        state, {"configurable": {"thread_id": new_id()}, "recursion_limit": recursion_limit_for(10)}
    )
    # A dead LLM must not crash the campaign — deterministic search still reaches the target.
    assert out["stop_reason"] == "target_reached"
