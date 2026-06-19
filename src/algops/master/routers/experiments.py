"""Experiment CRUD + run trigger + final result."""

from __future__ import annotations

import asyncio

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy.orm import Session

from algops.core import ExperimentCreate, ExperimentOut, FinalResultOut, MetricName
from algops.core.enums import TERMINAL_STATUSES, ExperimentStatus
from algops.tracking import get_session, repo
from algops.tracking.models import Experiment

from ..loop import resume_experiment_loop, run_experiment_loop

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])


def _to_out(e: Experiment) -> ExperimentOut:
    return ExperimentOut(
        experiment_id=e.experiment_id,
        project_id=e.project_id,
        name=e.name,
        goal_text=e.goal_text,
        target_metric=MetricName(e.target_metric),
        target_value=e.target_value,
        goal_direction=e.goal_direction,
        max_iteration=e.max_iteration,
        status=e.status,
        best_run_id=e.best_run_id,
    )


@router.post("", response_model=ExperimentOut)
def create_experiment(body: ExperimentCreate) -> ExperimentOut:
    with get_session() as s:
        if repo.get_project(s, body.project_id) is None:
            raise HTTPException(404, "project not found")
        if body.primary_param_key is None:
            proj = repo.get_project(s, body.project_id)
            spec = (proj.param_spec or {}).get("params") if proj else None
            if spec:
                body.primary_param_key = spec[0]["key"]
        return _to_out(repo.create_experiment(s, body))


@router.get("", response_model=list[ExperimentOut])
def list_experiments(project_id: str | None = None) -> list[ExperimentOut]:
    with get_session() as s:
        return [_to_out(e) for e in repo.list_experiments(s, project_id)]


@router.get("/{experiment_id}", response_model=ExperimentOut)
def get_experiment(experiment_id: str) -> ExperimentOut:
    with get_session() as s:
        e = repo.get_experiment(s, experiment_id)
        if e is None:
            raise HTTPException(404, "experiment not found")
        return _to_out(e)


@router.post("/{experiment_id}/run")
async def run_experiment(experiment_id: str, request: Request) -> dict:
    settings = request.app.state.settings
    with get_session() as s:
        e = repo.get_experiment(s, experiment_id)
        if e is None:
            raise HTTPException(404, "experiment not found")
        if e.status == ExperimentStatus.running.value:
            raise HTTPException(409, "experiment already running")
        # Reset terminal experiments to allow re-running.
        if e.status in {st.value for st in TERMINAL_STATUSES}:
            repo.set_experiment_status(s, experiment_id, ExperimentStatus.created.value)

    if settings.dev_mode:
        task = asyncio.create_task(
            run_experiment_loop(
                experiment_id=experiment_id,
                bus=request.app.state.bus,
                ws_broker=request.app.state.ws,
                artifact_store=request.app.state.store,
                settings=settings,
                rag_store=request.app.state.rag,
            )
        )
        request.app.state.tasks.add(task)
        task.add_done_callback(request.app.state.tasks.discard)
    else:
        from ..celery_app import run_experiment_task

        run_experiment_task.delay(experiment_id)

    return {"experiment_id": experiment_id, "status": "started"}


@router.post("/{experiment_id}/approve")
async def approve_experiment(experiment_id: str, request: Request, approved: bool = True) -> dict:
    """Resume a HITL-paused experiment (require_approval). approved=false stops it."""
    settings = request.app.state.settings
    with get_session() as s:
        e = repo.get_experiment(s, experiment_id)
        if e is None:
            raise HTTPException(404, "experiment not found")
        if e.status != ExperimentStatus.paused.value:
            raise HTTPException(409, "experiment is not awaiting approval")

    if not approved:
        with get_session() as s:
            repo.set_experiment_status(s, experiment_id, ExperimentStatus.stopped_by_pm.value)
        await request.app.state.ws.publish(
            experiment_id, {"type": "experiment_done", "experiment_id": experiment_id,
                            "status": ExperimentStatus.stopped_by_pm.value, "stop_reason": "rejected"}
        )
        return {"experiment_id": experiment_id, "status": "rejected"}

    if settings.dev_mode:
        task = asyncio.create_task(
            resume_experiment_loop(
                experiment_id=experiment_id, approved=True,
                bus=request.app.state.bus, ws_broker=request.app.state.ws,
                artifact_store=request.app.state.store, settings=settings,
                rag_store=request.app.state.rag,
            )
        )
        request.app.state.tasks.add(task)
        task.add_done_callback(request.app.state.tasks.discard)
    else:
        from ..celery_app import resume_experiment_task

        resume_experiment_task.delay(experiment_id, True)
    return {"experiment_id": experiment_id, "status": "resumed"}


@router.get("/{experiment_id}/final", response_model=FinalResultOut)
def get_final(experiment_id: str) -> FinalResultOut:
    with get_session() as s:
        fr = repo.get_final_result(s, experiment_id)
        if fr is None:
            raise HTTPException(404, "no final result yet")
        from algops.core import Metrics

        return FinalResultOut(
            experiment_id=fr.experiment_id,
            winning_run_id=fr.winning_run_id,
            optimal_params=fr.optimal_params or {},
            metrics=Metrics(**fr.metrics) if fr.metrics else None,
            stop_reason=fr.stop_reason,
            report_md_uri=fr.report_md_uri,
            notified=fr.notified,
        )
