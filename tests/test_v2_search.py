"""Multi-parameter search strategies + a full campaign through the real runner protocol."""

from __future__ import annotations

import os

from langgraph.checkpoint.memory import MemorySaver

from algops.agents import EngineRunExecutor, LoopDeps, NullHooks, build_graph, initial_state, recursion_limit_for
from algops.core import Constraint, GoalDirection, Objective, ParamSpec, load_manifest, new_id
from algops.runner import RunnerEngine
from algops.search import params_key, propose

from .conftest import EXAMPLES

SPEC = ParamSpec(params=[
    {"key": "q.x", "dtype": "float", "default": 0.0, "range": [-1, 1], "step": 0.25, "stage": "s"},
    {"key": "q.y", "dtype": "int", "default": 0, "range": [-4, 4], "step": 1, "stage": "s"},
    {"key": "q.m", "dtype": "enum", "default": "a", "enum": ["a", "b", "c"], "stage": "s"},
])
OBJ = Objective(metric="loss", direction=GoalDirection.minimize, target=0.0)


def _loss(p: dict) -> float:
    return (p["q.x"] - 0.5) ** 2 + (p["q.y"] - 2) ** 2 + (0.0 if p["q.m"] == "c" else 0.3)


def _drive(strategy: str, k: int, budget: int) -> list[dict]:
    history: list[dict] = []
    base = SPEC.effective()
    history.append({"params": base, "metrics": {"loss": _loss(base)}, "status": "evaluated", "run_id": "r0"})
    state: dict = {}
    for i in range(budget):
        cands, state, converged = propose(strategy, SPEC, ["q.x", "q.y", "q.m"], OBJ, history, state, k, seed=1)
        if converged or not cands:
            break
        assert len(cands) <= k
        for j, c in enumerate(cands):
            history.append({"params": c, "metrics": {"loss": _loss(c)}, "status": "evaluated", "run_id": f"r{i}-{j}"})
    return history


def test_coordinate_search_finds_the_optimum_and_never_repeats():
    h = _drive("coordinate", 2, 40)
    keys = [params_key(e["params"]) for e in h]
    assert len(keys) == len(set(keys))
    best = min(h, key=lambda e: e["metrics"]["loss"])
    assert best["params"] == {"q.x": 0.5, "q.y": 2, "q.m": "c"}


def test_grid_and_random_exhaust_the_space_without_duplicates():
    for strategy in ("grid", "random"):
        h = _drive(strategy, 8, 200)
        keys = [params_key(e["params"]) for e in h]
        assert len(keys) == len(set(keys))
        assert len(h) == 9 * 9 * 3  # 9 x-values, 9 y-values, 3 enum choices
        assert min(e["metrics"]["loss"] for e in h) == 0.0


def test_constraints_rank_feasible_first():
    obj = Objective(metric="loss", direction=GoalDirection.minimize,
                    constraints=[Constraint(metric="q_y", op=">=", value=3)])
    h = [{"params": {"q.x": 0.5, "q.y": 2, "q.m": "c"}, "metrics": {"loss": 0.0, "q_y": 2}, "status": "evaluated"},
         {"params": {"q.x": 0.5, "q.y": 3, "q.m": "c"}, "metrics": {"loss": 1.0, "q_y": 3}, "status": "evaluated"}]
    from algops.search import best_entry

    assert best_entry(h, obj)["params"]["q.y"] == 3


async def test_campaign_through_runner_protocol_with_constraints_batch_and_validation(tmp_path):
    root = os.path.join(EXAMPLES, "py-multi-algo")
    manifest = load_manifest(root)
    algo = manifest.algorithm("detector")
    engine = RunnerEngine(str(tmp_path / "w"), local_projects={"py": root})
    deps = LoopDeps(executor=EngineRunExecutor(engine, "py"), hooks=NullHooks())
    graph = build_graph(deps, checkpointer=MemorySaver())
    obj = algo.default_objective()  # fp_rate <= 0.08 s.t. recall >= 0.7
    state = initial_state(
        experiment_id="e", project_id="p", project_name="py", goal_text="오탐 완화",
        target_metric=obj.metric, target_value=obj.target, goal_direction=obj.direction.value,
        max_iteration=12, param_spec=algo.spec().model_dump(mode="json"), objective=obj,
        algorithm_id="detector", suite_id="det-smoke", validation_suite_ids=["det-full"],
        strategy="coordinate", batch_size=3, reason_map=algo.spec().reason_map(),
    )
    out = await graph.ainvoke(state, {"configurable": {"thread_id": new_id()},
                                      "recursion_limit": recursion_limit_for(12)})
    search = [h for h in out["history"] if h["kind"] == "search"]
    assert search[0]["params"] == algo.spec().effective()  # iteration 1 = the repo's defaults
    assert max(sum(1 for h in search if h["iteration"] == i) for i in range(1, out["iteration"] + 1)) == 3
    best = out["final_metrics"]
    assert best["recall"] >= 0.7  # the constraint holds for the winner
    assert best["fp_rate"] < search[0]["metrics"]["fp_rate"]  # and it beats the defaults
    v = out["validation"]["det-full"]
    assert v["best"]["status"] == "evaluated" and v["baseline"]["status"] == "evaluated"
    assert v["best"]["metrics"]["fp_rate"] < v["baseline"]["metrics"]["fp_rate"]  # holds out-of-sample
    assert out["csv_sensitivity"] or out["csv_drop_reasons"]  # telemetry was aggregated
