"""Campaign (experiment) worker: pin a revision, wire deps + checkpointer, drive the graph.

A fresh run (or re-run) starts a new *attempt* with a clean LangGraph thread
(``thread_id = "<experiment_id>:<attempt>"``) pinned to the commit resolved at that moment —
so re-running never mixes histories and every evaluation of a campaign uses the same code.
A HITL resume re-enters the same attempt's thread from the Postgres/SQLite checkpoint.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from algops.agents import LoopDeps, build_graph, initial_state, recursion_limit_for
from algops.agents.executor import RunExecutor
from algops.core import (
    MasterSettings,
    Objective,
    RepoManifest,
    RepoRef,
    RunRequest,
    RunResult,
)
from algops.core.enums import ExperimentStatus
from algops.tracking import get_session, repo

from .bus import CommandBus
from .context import MasterContext, build_context
from .dispatch import Dispatcher, repo_ref
from .hooks import DbHooks
from .services.projects import latest_manifest, sync_project

_STOP_TO_STATUS = {
    "target_reached": ExperimentStatus.target_reached,
    "max_iteration": ExperimentStatus.stopped_max_iter,
    "converged": ExperimentStatus.stopped_by_pm,
    "pm_stop": ExperimentStatus.stopped_by_pm,
    "error": ExperimentStatus.failed,
}


class DispatcherRunExecutor(RunExecutor):
    """Pin every run of a campaign to (repo, sha) and dispatch it to an eligible runner."""

    def __init__(self, dispatcher: Dispatcher, ref: RepoRef, manifest: RepoManifest) -> None:
        self.dispatcher = dispatcher
        self.ref = ref
        self.manifest = manifest

    async def run(self, req: RunRequest) -> RunResult:
        algo = self.manifest.algorithm(req.algorithm_id)
        req = req.model_copy(update={"repo": self.ref, "timeout_s": req.timeout_s or algo.timeout_s})
        timeout = algo.timeout_s + self.manifest.env.setup_timeout_s + 120.0
        res, slave_id = await self.dispatcher.run(req, runs_on=algo.runs_on, timeout=timeout)
        if slave_id:
            res = res.model_copy(update={"extra": {**res.extra, "slave_id": slave_id}})
        return res


class CommandBusRunExecutor(RunExecutor):
    """v1: dispatch to one known slave (kept for compatibility)."""

    def __init__(self, bus: CommandBus, slave_id: str, timeout: float) -> None:
        self.bus = bus
        self.slave_id = slave_id
        self.timeout = timeout

    async def run(self, req: RunRequest) -> RunResult:
        return await self.bus.submit_run(self.slave_id, req, self.timeout)


@asynccontextmanager
async def _checkpointer(settings: MasterSettings):
    if settings.checkpoint_backend == "postgres":
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver

        async with AsyncPostgresSaver.from_conn_string(settings.db_url) as cp:
            await cp.setup()
            yield cp
    else:
        import os

        from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

        os.makedirs(os.path.dirname(os.path.abspath(settings.checkpoint_db)) or ".", exist_ok=True)
        async with AsyncSqliteSaver.from_conn_string(settings.checkpoint_db) as cp:
            yield cp


def _ctx_from(ctx: MasterContext | None, bus, ws_broker, artifact_store, settings, rag_store) -> MasterContext:
    if ctx is not None:
        return ctx
    return build_context(settings, bus=bus, ws=ws_broker, store=artifact_store, rag=rag_store)


async def run_experiment_loop(
    *,
    experiment_id: str,
    ctx: MasterContext | None = None,
    bus=None,
    ws_broker=None,
    artifact_store=None,
    settings: MasterSettings | None = None,
    rag_store=None,
    resume_value=None,
) -> None:
    ctx = _ctx_from(ctx, bus, ws_broker, artifact_store, settings or MasterSettings(), rag_store)
    ws = ctx.ws
    with get_session() as s:
        exp = repo.get_experiment(s, experiment_id)
        if exp is None:
            return
        project = repo.get_project(s, exp.project_id)
        if project is None:
            return
        e = {c.name: getattr(exp, c.name) for c in exp.__table__.columns}
        pname = project.name

    # ---- pin the revision (fresh attempt) or reuse it (HITL resume) -----------------------
    if resume_value is None:
        rev, err = await sync_project(ctx, e["project_id"], e["ref"])
        if rev is None:
            rev, _ = latest_manifest(e["project_id"])
            if rev is None or e["ref"]:
                await _fail(ws, experiment_id, f"프로젝트 동기화 실패: {err}")
                return
        sha = rev.sha
        with get_session() as s:
            ex = repo.get_experiment(s, experiment_id)
            if ex is not None:
                ex.commit_sha = sha
    else:
        sha = e["commit_sha"]
    rev, manifest = latest_manifest(e["project_id"], sha=sha)
    if manifest is None:
        await _fail(ws, experiment_id, f"리비전 {str(sha)[:12]}의 매니페스트를 읽을 수 없습니다")
        return
    try:
        algo = manifest.algorithm(e["algorithm_id"])
        suite_ids = [su.id for su in manifest.suites_for(algo.id)]
        if e["suite_id"] not in suite_ids:
            raise KeyError(f"suite {e['suite_id']!r} is not enabled for {algo.id!r} (have {suite_ids})")
        for vs in e["validation_suite_ids"] or []:
            manifest.suite(vs)
    except KeyError as exc:
        await _fail(ws, experiment_id, str(exc).strip("'\""))
        return
    spec = algo.spec()
    if not spec.params:
        await _fail(ws, experiment_id, f"알고리즘 {algo.id!r}에 탐색할 파라미터가 없습니다")
        return
    keys = [k for k in (e["search_keys"] or []) if k in spec.by_key()] or [p.key for p in spec.params]
    unknown = [k for k in (e["search_keys"] or []) if k not in spec.by_key()]
    if unknown:
        await _fail(ws, experiment_id, f"알 수 없는 파라미터: {unknown}")
        return
    objective = Objective.model_validate(e["objective"]) if e["objective"] else Objective(
        metric=e["target_metric"], direction=e["goal_direction"], target=e["target_value"])
    suite = manifest.suite(e["suite_id"])
    attempt = int(e["attempt"] or 0)

    with get_session() as s:
        repo.set_experiment_status(s, experiment_id, ExperimentStatus.running.value)
    await ws.publish(experiment_id, {"type": "experiment_started", "experiment_id": experiment_id,
                                     "attempt": attempt, "commit_sha": sha})

    hooks = DbHooks(
        experiment_id=experiment_id, project_id=e["project_id"], project_name=pname,
        experiment_name=e["name"], goal_text=e["goal_text"], target_metric=objective.metric,
        target_value=objective.target, direction=objective.direction.value,
        artifact_store=ctx.store, ws_broker=ws, settings=ctx.settings, rag_store=ctx.rag,
        objective=objective, algorithm_id=algo.id, suite_id=suite.id, commit_sha=sha, attempt=attempt,
    )
    with get_session() as s:
        proj = repo.get_project(s, e["project_id"])
        ref = repo_ref(proj, sha=sha)
    assert ctx.dispatcher is not None
    executor = DispatcherRunExecutor(ctx.dispatcher, ref, manifest)
    planner = _build_planner(ctx.settings)
    deps = LoopDeps(executor=executor, hooks=hooks, planner=planner)
    state = initial_state(
        experiment_id=experiment_id, project_id=e["project_id"], project_name=pname,
        goal_text=e["goal_text"], target_metric=objective.metric, target_value=objective.target,
        goal_direction=objective.direction.value, max_iteration=e["max_iteration"],
        test_set_uri=suite.uri, primary_param_key=e["primary_param_key"],
        param_spec=spec.model_dump(mode="json"), require_approval=ctx.settings.require_approval,
        objective=objective, algorithm_id=algo.id, suite_id=suite.id,
        validation_suite_ids=e["validation_suite_ids"] or [], strategy=e["strategy"] or "coordinate",
        search_keys=keys, batch_size=e["batch_size"] or 1, seed=e["seed"] or 0, commit_sha=sha,
        attempt=attempt, reason_map=spec.reason_map(),
    )
    config = {
        "configurable": {"thread_id": e["langgraph_thread_id"]},
        "recursion_limit": recursion_limit_for(e["max_iteration"]),
    }
    from langgraph.types import Command

    graph_input = Command(resume=resume_value) if resume_value is not None else state
    try:
        async with _checkpointer(ctx.settings) as cp:
            graph = build_graph(deps, checkpointer=cp)
            out = await graph.ainvoke(graph_input, config)
    except Exception as exc:  # noqa: BLE001
        await _fail(ws, experiment_id, f"loop error: {type(exc).__name__}: {exc}")
        return

    interrupts = out.get("__interrupt__")
    if interrupts:  # HITL: the Developer paused for approval — surface it and wait for /approve.
        payload = getattr(interrupts[0], "value", {})
        with get_session() as s:
            repo.set_experiment_status(s, experiment_id, ExperimentStatus.paused.value)
        await ws.publish(experiment_id, {"type": "approval_required", "experiment_id": experiment_id, **payload})
        return

    stop_reason = out.get("stop_reason")
    status = _STOP_TO_STATUS.get(stop_reason, ExperimentStatus.stopped_by_pm)
    error = None
    if status is ExperimentStatus.failed:
        failed = [h for h in out.get("history", []) if h.get("status") != "evaluated"]
        if failed:
            error = f"[{failed[-1].get('error_kind')}] {failed[-1].get('error')}"
    with get_session() as s:
        repo.set_experiment_status(s, experiment_id, status.value, error=error)
        if out.get("winning_run_id"):
            repo.set_best_run(s, experiment_id, out["winning_run_id"])
    await ws.publish(experiment_id, {
        "type": "experiment_done", "experiment_id": experiment_id, "status": status.value,
        "stop_reason": stop_reason, "winning_run_id": out.get("winning_run_id"), "error": error,
    })


async def resume_experiment_loop(*, experiment_id: str, approved: bool, ctx: MasterContext | None = None,
                                 bus=None, ws_broker=None, artifact_store=None, settings=None,
                                 rag_store=None) -> None:
    """Resume a HITL-paused experiment after human approval (re-enters the interrupted graph)."""
    await run_experiment_loop(
        experiment_id=experiment_id, ctx=ctx, bus=bus, ws_broker=ws_broker,
        artifact_store=artifact_store, settings=settings, rag_store=rag_store,
        resume_value=bool(approved),
    )


def _build_planner(settings: MasterSettings):
    """Build the LLM planner from settings.llm; None (deterministic) on mode='none' or error."""
    try:
        from algops.llm import OpenAILLMPlanner, build_chat

        chat = build_chat(settings.llm)
        return OpenAILLMPlanner(chat) if chat is not None else None
    except Exception as exc:  # noqa: BLE001 — extra missing / bad config: degrade to deterministic
        print(f"[loop] LLM planner unavailable, using deterministic search: {exc}", flush=True)
        return None


async def _fail(ws_broker, experiment_id: str, message: str) -> None:
    with get_session() as s:
        repo.set_experiment_status(s, experiment_id, ExperimentStatus.failed.value, error=message)
    await ws_broker.publish(
        experiment_id, {"type": "experiment_failed", "experiment_id": experiment_id, "error": message}
    )
