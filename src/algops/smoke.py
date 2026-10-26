"""``uv run algops-smoke`` — in-process end-to-end demo (no Docker, no separate runner process).

Drives the real LangGraph campaign loop + tracking DB + artifact store + finalize/notify against
the bundled toy detector, executed through the real run protocol (subprocess + request.json /
result.json) by a local RunnerEngine. The distributed master + runner flow is exercised by
tests/test_e2e_distributed.py and tests/test_v2_e2e.py.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from langgraph.checkpoint.memory import MemorySaver

from algops.agents import EngineRunExecutor, LoopDeps, build_graph, initial_state, recursion_limit_for
from algops.core import MasterSettings, ProjectCreate, load_manifest
from algops.master.hooks import DbHooks
from algops.rag import InMemoryRagStore
from algops.runner import RunnerEngine, bundled_toy_path
from algops.tracking import get_artifact_store, get_session, init_engine, repo


class _PrintBroker:
    async def publish(self, channel: str, message: dict) -> None:
        if message.get("type") == "run_evaluated":
            m = message["metrics"]
            thr = message["params"].get("detector.confidence_threshold")
            print(f"  · iter {message['iteration']:>2}  threshold={thr:.3f}  "
                  f"fp_rate={m.get('fp_rate', float('nan')):.3f}  recall={m.get('recall', float('nan')):.3f}",
                  flush=True)


async def _main() -> None:
    tmp = tempfile.mkdtemp(prefix="algops_smoke_")
    settings = MasterSettings(
        db_url=f"sqlite+pysqlite:///{os.path.join(tmp, 'tracking.sqlite')}",
        artifact_dir=os.path.join(tmp, "artifacts"),
        dev_mode=True,
    )
    init_engine(settings)
    store = get_artifact_store(settings)
    toy = bundled_toy_path()
    algo = load_manifest(toy).algorithm("default")
    obj = algo.default_objective()
    assert obj is not None
    with get_session() as s:
        pid = repo.create_project(s, ProjectCreate(name="toy-project"), repo_kind="local").project_id
        eid = repo.create_experiment(
            s, project_id=pid, name="오탐 완화 실험", goal_text="오탐 완화", target_metric=obj.metric,
            target_value=obj.target, goal_direction=obj.direction.value, max_iteration=10,
            objective=obj.model_dump(mode="json"),
        ).experiment_id

    print(f"프로젝트={pid[:8]}  실험={eid[:8]}  목표: {obj.describe()}\n실험 진행:")
    hooks = DbHooks(
        experiment_id=eid, project_id=pid, project_name="toy-project", experiment_name="오탐 완화 실험",
        goal_text="오탐 완화", target_metric=obj.metric, target_value=obj.target,
        direction=obj.direction.value, artifact_store=store, ws_broker=_PrintBroker(),
        settings=settings, rag_store=InMemoryRagStore(), objective=obj,
    )
    engine = RunnerEngine(os.path.join(tmp, "runner"), local_projects={"toy-project": toy})
    deps = LoopDeps(executor=EngineRunExecutor(engine, "toy-project"), hooks=hooks)
    graph = build_graph(deps, checkpointer=MemorySaver())
    state = initial_state(
        experiment_id=eid, project_id=pid, project_name="toy-project", goal_text="오탐 완화",
        target_metric=obj.metric, target_value=obj.target, goal_direction=obj.direction.value,
        max_iteration=10, param_spec=algo.spec().model_dump(mode="json"), objective=obj,
        search_keys=["detector.confidence_threshold"], reason_map=algo.spec().reason_map(),
    )
    out = await graph.ainvoke(state, {"configurable": {"thread_id": f"{eid}:0"},
                                      "recursion_limit": recursion_limit_for(10)})
    with get_session() as s:
        fr = repo.get_final_result(s, eid)
        runs = repo.list_runs(s, eid)
    print(f"\n종료 사유: {out['stop_reason']}  ·  반복 {out['iteration']}회  ·  DB run {len(runs)}건")
    print(f"최적 파라미터: {fr.optimal_params}  (기본값 대비 변경: {fr.changed_params})")
    print(f"최적 fp_rate: {fr.metrics['fp_rate']:.4f}  recall: {fr.metrics['recall']:.4f}")
    print(f"알림 발송: {fr.notified}  ·  리포트: {fr.report_md_uri}")
    print(f"\n(임시 데이터: {tmp})")


def main() -> None:
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # readable Korean on cp949 consoles
    except Exception:  # noqa: BLE001
        pass
    asyncio.run(_main())


if __name__ == "__main__":
    main()
