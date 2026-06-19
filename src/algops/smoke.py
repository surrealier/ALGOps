"""``uv run algops-smoke`` — in-process end-to-end demo (no Docker, no separate slave process).

Drives the real LangGraph loop + tracking DB + artifact store + finalize/notify against the
bundled toy detector via a LocalRunExecutor. The fully-distributed master+slave flow is
exercised by tests/test_e2e_distributed.py.
"""

from __future__ import annotations

import asyncio
import os
import tempfile

from langgraph.checkpoint.memory import MemorySaver

from algops.agents import LoopDeps, LocalRunExecutor, build_graph, initial_state, recursion_limit_for
from algops.core import ExperimentCreate, MasterSettings, MetricName, ProjectCreate
from algops.master.hooks import DbHooks
from algops.tracking import get_artifact_store, get_session, init_engine, repo


class _NullBroker:
    async def publish(self, experiment_id: str, message: dict) -> None:
        kind = message.get("type")
        if kind == "run_evaluated":
            m = message["metrics"]
            print(
                f"  · iter {message['iteration']:>2}  "
                f"threshold={message['params'].get('detector.confidence_threshold'):.3f}  "
                f"fp_rate={m['fp_rate']:.3f}  recall={m['recall']:.3f}  f1={m['f1']:.3f}",
                flush=True,
            )


def _local_run(req):
    from algops.toy_project import run as toy_run

    work = os.path.join(tempfile.gettempdir(), "algops_smoke", req.run_id)
    return toy_run(req, work)


async def _main() -> None:
    tmp = tempfile.mkdtemp(prefix="algops_smoke_")
    settings = MasterSettings(
        db_url=f"sqlite+pysqlite:///{os.path.join(tmp, 'tracking.sqlite')}",
        artifact_dir=os.path.join(tmp, "artifacts"),
        dev_mode=True,
    )
    init_engine(settings)
    store = get_artifact_store(settings)

    from algops.toy_project import load_param_spec

    spec = load_param_spec()
    with get_session() as s:
        proj = repo.create_project(s, ProjectCreate(name="toy-project", modality="tabular"),
                                   param_spec=spec.model_dump())
        pid = proj.project_id
        exp = repo.create_experiment(
            s,
            ExperimentCreate(
                project_id=pid, name="오탐 완화 실험", goal_text="오탐 완화",
                target_metric=MetricName.fp_rate, target_value=0.05, max_iteration=10,
                primary_param_key="detector.confidence_threshold",
            ),
        )
        eid = exp.experiment_id

    print(f"프로젝트={pid[:8]}  실험={eid[:8]}  목표: fp_rate ≤ 0.05\n실험 진행:")
    from algops.rag import InMemoryRagStore

    hooks = DbHooks(
        experiment_id=eid, project_id=pid, project_name="toy-project", experiment_name="오탐 완화 실험",
        goal_text="오탐 완화", target_metric="fp_rate", target_value=0.05, direction="minimize",
        artifact_store=store, ws_broker=_NullBroker(), settings=settings, rag_store=InMemoryRagStore(),
    )
    deps = LoopDeps(executor=LocalRunExecutor(_local_run), hooks=hooks)
    graph = build_graph(deps, checkpointer=MemorySaver())
    state = initial_state(
        experiment_id=eid, project_id=pid, project_name="toy-project", goal_text="오탐 완화",
        target_metric="fp_rate", target_value=0.05, goal_direction="minimize", max_iteration=10,
        test_set_uri="bundled://toy", primary_param_key="detector.confidence_threshold",
        param_spec=spec.model_dump(),
    )
    out = await graph.ainvoke(
        state,
        {"configurable": {"thread_id": eid}, "recursion_limit": recursion_limit_for(10)},
    )

    with get_session() as s:
        fr = repo.get_final_result(s, eid)
        runs = repo.list_runs(s, eid)
    print(f"\n종료 사유: {out['stop_reason']}  ·  반복 {out['iteration']}회  ·  DB run {len(runs)}건")
    print(f"최적 파라미터: {fr.optimal_params}")
    print(f"최적 fp_rate: {fr.metrics['fp_rate']:.4f}  recall: {fr.metrics['recall']:.4f}")
    print(f"알림 발송: {fr.notified}  ·  리포트: {fr.report_md_uri}")
    print(f"\n(임시 데이터: {tmp})")


def main() -> None:
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")  # readable Korean on cp949 consoles
    except Exception:
        pass
    asyncio.run(_main())


if __name__ == "__main__":
    main()
