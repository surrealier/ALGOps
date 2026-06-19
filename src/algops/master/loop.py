"""The experiment-loop worker entrypoint: wire deps + checkpointer and drive the graph.

In dev this runs in-process (asyncio task) so it shares the API's event loop, CommandBus, and
WsBroker. In prod it runs in a Celery worker; the durability layer is the LangGraph Postgres
checkpoint (``thread_id == experiment_id``), so a worker restart resumes mid-campaign.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from algops.agents import (
    LoopDeps,
    build_graph,
    initial_state,
    recursion_limit_for,
)
from algops.agents.executor import RunExecutor
from algops.core import MasterSettings, ParamSpec, RunRequest, RunResult
from algops.core.enums import ExperimentStatus
from algops.tracking import get_session, repo

from .bus import CommandBus
from .hooks import DbHooks

_STOP_TO_STATUS = {
    "target_reached": ExperimentStatus.target_reached,
    "max_iteration": ExperimentStatus.stopped_max_iter,
    "converged": ExperimentStatus.stopped_by_pm,
    "pm_stop": ExperimentStatus.stopped_by_pm,
}


class CommandBusRunExecutor(RunExecutor):
    """Dispatch a run to a slave via the command bus and await the result it posts back."""

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


def _resolve_param_spec(project_param_spec: dict | None) -> ParamSpec:
    if project_param_spec:
        return ParamSpec(**project_param_spec)
    from algops.toy_project import load_param_spec  # bundled fallback

    return load_param_spec()


async def run_experiment_loop(
    *,
    experiment_id: str,
    bus: CommandBus,
    ws_broker,
    artifact_store,
    settings: MasterSettings,
    rag_store=None,
    resume_value=None,
) -> None:
    with get_session() as s:
        exp = repo.get_experiment(s, experiment_id)
        if exp is None:
            return
        project = repo.get_project(s, exp.project_id)
        ctx = {
            "project_id": exp.project_id,
            "project_name": project.name if project else "project",
            "experiment_name": exp.name,
            "goal_text": exp.goal_text,
            "target_metric": exp.target_metric,
            "target_value": exp.target_value,
            "goal_direction": exp.goal_direction,
            "max_iteration": exp.max_iteration,
            "test_set_uri": exp.test_set_uri,
            "primary_param_key": exp.primary_param_key,
            "param_spec": project.param_spec if project else None,
        }

    spec = _resolve_param_spec(ctx["param_spec"])
    primary_key = ctx["primary_param_key"] or (spec.params[0].key if spec.params else None)
    if primary_key is None:
        await _fail(ws_broker, experiment_id, "no parameters to optimize (empty param spec)")
        return

    slave_id = _online_slave(ctx["project_id"])
    if slave_id is None:
        await _fail(ws_broker, experiment_id, "연결된 온라인 Slave가 없습니다 (프로젝트에 Slave를 띄워 등록하세요)")
        return

    with get_session() as s:
        repo.set_experiment_status(s, experiment_id, ExperimentStatus.running.value)
    await ws_broker.publish(experiment_id, {"type": "experiment_started", "experiment_id": experiment_id})

    hooks = DbHooks(
        experiment_id=experiment_id,
        project_id=ctx["project_id"],
        project_name=ctx["project_name"],
        experiment_name=ctx["experiment_name"],
        goal_text=ctx["goal_text"],
        target_metric=ctx["target_metric"],
        target_value=ctx["target_value"],
        direction=ctx["goal_direction"],
        artifact_store=artifact_store,
        ws_broker=ws_broker,
        settings=settings,
        rag_store=rag_store,
    )
    executor = CommandBusRunExecutor(bus, slave_id, settings.command_timeout_s)
    planner = _build_planner(settings, ws_broker, experiment_id)
    deps = LoopDeps(executor=executor, hooks=hooks, planner=planner)

    state = initial_state(
        experiment_id=experiment_id,
        project_id=ctx["project_id"],
        project_name=ctx["project_name"],
        goal_text=ctx["goal_text"],
        target_metric=ctx["target_metric"],
        target_value=ctx["target_value"],
        goal_direction=ctx["goal_direction"],
        max_iteration=ctx["max_iteration"],
        test_set_uri=ctx["test_set_uri"],
        primary_param_key=primary_key,
        param_spec=spec.model_dump(),
        require_approval=settings.require_approval,
    )
    config = {
        "configurable": {"thread_id": experiment_id},
        "recursion_limit": recursion_limit_for(ctx["max_iteration"]),
    }

    # Fresh run uses the initial state; a resume feeds the human's approval back into the
    # interrupted Developer node.
    from langgraph.types import Command

    graph_input = Command(resume=resume_value) if resume_value is not None else state

    try:
        async with _checkpointer(settings) as cp:
            graph = build_graph(deps, checkpointer=cp)
            out = await graph.ainvoke(graph_input, config)
    except Exception as exc:  # noqa: BLE001
        await _fail(ws_broker, experiment_id, f"loop error: {exc}")
        return

    # HITL: the Developer paused for approval — surface it and wait for /approve.
    interrupts = out.get("__interrupt__")
    if interrupts:
        payload = getattr(interrupts[0], "value", {})
        with get_session() as s:
            repo.set_experiment_status(s, experiment_id, ExperimentStatus.paused.value)
        await ws_broker.publish(
            experiment_id, {"type": "approval_required", "experiment_id": experiment_id, **payload}
        )
        return

    stop_reason = out.get("stop_reason")
    status = _STOP_TO_STATUS.get(stop_reason, ExperimentStatus.stopped_by_pm)
    with get_session() as s:
        repo.set_experiment_status(s, experiment_id, status.value)
        if out.get("winning_run_id"):
            repo.set_best_run(s, experiment_id, out["winning_run_id"])
    await ws_broker.publish(
        experiment_id,
        {"type": "experiment_done", "experiment_id": experiment_id, "status": status.value,
         "stop_reason": stop_reason, "winning_run_id": out.get("winning_run_id")},
    )


async def resume_experiment_loop(
    *, experiment_id: str, approved: bool, bus, ws_broker, artifact_store, settings, rag_store=None
) -> None:
    """Resume a HITL-paused experiment after human approval (re-enters the interrupted graph)."""
    await run_experiment_loop(
        experiment_id=experiment_id, bus=bus, ws_broker=ws_broker,
        artifact_store=artifact_store, settings=settings, rag_store=rag_store,
        resume_value=bool(approved),
    )


def _build_planner(settings: MasterSettings, ws_broker, experiment_id: str):
    """Build the LLM planner from settings.llm; None (deterministic) on mode='none' or error."""
    try:
        from algops.llm import OpenAILLMPlanner, build_chat

        chat = build_chat(settings.llm)
        return OpenAILLMPlanner(chat) if chat is not None else None
    except Exception as exc:  # extra missing / bad config — degrade to deterministic
        print(f"[loop] LLM planner unavailable, using deterministic search: {exc}", flush=True)
        return None


def _online_slave(project_id: str) -> str | None:
    with get_session() as s:
        slave = repo.online_slave_for_project(s, project_id)
        return slave.slave_id if slave else None


async def _fail(ws_broker, experiment_id: str, message: str) -> None:
    with get_session() as s:
        repo.set_experiment_status(s, experiment_id, ExperimentStatus.failed.value)
    await ws_broker.publish(
        experiment_id, {"type": "experiment_failed", "experiment_id": experiment_id, "error": message}
    )
