"""Campaigns (experiments): create on one algorithm, run / re-run, HITL approve, results, promote."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from algops.core import (
    ExperimentCreate,
    ExperimentOut,
    FinalResultOut,
    GoalDirection,
    MetricName,
    Objective,
    PromoteIn,
    resolve_direction,
)
from algops.core.enums import ExperimentStatus
from algops.tracking import get_session, repo
from algops.tracking.models import Experiment

from ..context import MasterContext
from ..loop import resume_experiment_loop, run_experiment_loop
from ..services.projects import latest_manifest, sync_project
from ..services.promotion import promote_experiment
from .deps import get_ctx, require_api

router = APIRouter(prefix="/api/v1/experiments", tags=["experiments"])


def _to_out(e: Experiment) -> ExperimentOut:
    return ExperimentOut(
        experiment_id=e.experiment_id,
        project_id=e.project_id,
        name=e.name,
        goal_text=e.goal_text,
        target_metric=e.target_metric,
        target_value=e.target_value,
        goal_direction=GoalDirection(e.goal_direction),
        max_iteration=e.max_iteration,
        status=e.status,
        best_run_id=e.best_run_id,
        algorithm_id=e.algorithm_id,
        suite_id=e.suite_id,
        validation_suite_ids=list(e.validation_suite_ids or []),
        objective=e.objective,
        strategy=e.strategy,
        search_keys=e.search_keys,
        batch_size=e.batch_size or 1,
        ref=e.ref,
        commit_sha=e.commit_sha,
        attempt=e.attempt or 0,
        created_at=repo.iso(e.created_at),
    )


@router.post("", response_model=ExperimentOut, dependencies=[Depends(require_api)])
async def create_experiment(body: ExperimentCreate, ctx: MasterContext = Depends(get_ctx)) -> ExperimentOut:
    with get_session() as s:
        proj = repo.find_project(s, body.project_id)
        if proj is None:
            raise HTTPException(404, "project not found")
        pid = proj.project_id
    rev, manifest = latest_manifest(pid)
    if manifest is None or body.ref:
        rev2, err = await sync_project(ctx, pid, body.ref)
        if rev2 is None and manifest is None:
            raise HTTPException(409, f"프로젝트를 동기화할 수 없습니다: {err}")
        if rev2 is not None:
            rev, manifest = latest_manifest(pid, rev2.sha)
    assert manifest is not None
    ids = [a.id for a in manifest.algorithms]
    algo_id = body.algorithm_id or ("default" if "default" in ids else ids[0])
    try:
        algo = manifest.algorithm(algo_id)
    except KeyError:
        raise HTTPException(400, f"unknown algorithm {algo_id!r} (have {ids})") from None
    suites = [x.id for x in manifest.suites_for(algo.id)]
    suite_id = body.suite_id or suites[0]
    if suite_id not in suites:
        raise HTTPException(400, f"suite {suite_id!r} is not enabled for {algo.id!r} (have {suites})")
    all_suites = [x.id for x in manifest.suites]
    bad = [v for v in body.validation_suite_ids if v not in all_suites]
    if bad:
        raise HTTPException(400, f"unknown validation suite(s) {bad}")
    validation = [v for v in body.validation_suite_ids if v != suite_id]
    spec = algo.spec()
    if not spec.params:
        raise HTTPException(400, f"algorithm {algo.id!r} declares no parameters to tune")
    keys = body.search_keys or ([body.primary_param_key] if body.primary_param_key else None)
    if keys:
        unknown = [k for k in keys if k not in spec.by_key()]
        if unknown:
            raise HTTPException(400, f"unknown parameter(s) {unknown}")

    declared = algo.metric_specs()
    legacy_fields = {"target_metric", "target_value", "goal_direction"} & body.model_fields_set
    if body.objective is not None:
        o = body.objective
        objective = Objective(metric=o.metric, direction=o.direction or resolve_direction(o.metric, declared),
                              target=o.target, constraints=o.constraints)
    elif legacy_fields:
        tm = body.target_metric
        objective = Objective(metric=tm, direction=body.goal_direction or resolve_direction(tm, declared),
                              target=body.target_value)
    else:
        objective = algo.default_objective() or Objective(
            metric=MetricName.fp_rate.value, direction=GoalDirection.minimize, target=0.1)
    suite = manifest.suite(suite_id)
    with get_session() as s:
        exp = repo.create_experiment(
            s,
            project_id=pid,
            name=body.name,
            goal_text=body.goal_text,
            target_metric=objective.metric,
            target_value=objective.target,
            goal_direction=objective.direction.value,
            max_iteration=body.max_iteration,
            primary_param_key=keys[0] if keys else None,
            test_set_uri=suite.uri,
            algorithm_id=algo.id,
            suite_id=suite_id,
            validation_suite_ids=validation,
            objective=objective.model_dump(mode="json"),
            strategy=body.strategy,
            search_keys=keys,
            batch_size=body.batch_size,
            seed=body.seed,
            ref=body.ref,
        )
        return _to_out(exp)


@router.get("", response_model=list[ExperimentOut])
def list_experiments(project_id: str | None = None) -> list[ExperimentOut]:
    with get_session() as s:
        if project_id:
            proj = repo.find_project(s, project_id)  # id or name
            if proj is None:
                return []
            project_id = proj.project_id
        return [_to_out(e) for e in repo.list_experiments(s, project_id)]


@router.get("/{experiment_id}", response_model=ExperimentOut)
def get_experiment(experiment_id: str) -> ExperimentOut:
    with get_session() as s:
        e = repo.get_experiment(s, experiment_id)
        if e is None:
            raise HTTPException(404, "experiment not found")
        return _to_out(e)


@router.post("/{experiment_id}/run", dependencies=[Depends(require_api)])
async def run_experiment(experiment_id: str, ctx: MasterContext = Depends(get_ctx)) -> dict:
    with get_session() as s:
        e = repo.get_experiment(s, experiment_id)
        if e is None:
            raise HTTPException(404, "experiment not found")
        if e.status == ExperimentStatus.running.value:
            raise HTTPException(409, "experiment already running")
        if e.status == ExperimentStatus.paused.value:
            raise HTTPException(409, "experiment is awaiting approval (POST /approve)")
        e = repo.start_attempt(s, experiment_id)
        e.status = ExperimentStatus.running.value
        attempt = e.attempt
    if ctx.settings.dev_mode:
        ctx.spawn(run_experiment_loop(experiment_id=experiment_id, ctx=ctx))
    else:  # pragma: no cover - requires Celery/Redis
        from ..celery_app import run_experiment_task

        run_experiment_task.delay(experiment_id)
    return {"experiment_id": experiment_id, "status": "started", "attempt": attempt}


@router.post("/{experiment_id}/approve", dependencies=[Depends(require_api)])
async def approve_experiment(experiment_id: str, approved: bool = True,
                             ctx: MasterContext = Depends(get_ctx)) -> dict:
    """Resume a HITL-paused experiment (require_approval). approved=false stops it."""
    with get_session() as s:
        e = repo.get_experiment(s, experiment_id)
        if e is None:
            raise HTTPException(404, "experiment not found")
        if e.status != ExperimentStatus.paused.value:
            raise HTTPException(409, "experiment is not awaiting approval")
    if not approved:
        with get_session() as s:
            repo.set_experiment_status(s, experiment_id, ExperimentStatus.stopped_by_pm.value)
        await ctx.ws.publish(experiment_id, {"type": "experiment_done", "experiment_id": experiment_id,
                                             "status": ExperimentStatus.stopped_by_pm.value,
                                             "stop_reason": "rejected"})
        return {"experiment_id": experiment_id, "status": "rejected"}
    with get_session() as s:
        repo.set_experiment_status(s, experiment_id, ExperimentStatus.running.value)
    if ctx.settings.dev_mode:
        ctx.spawn(resume_experiment_loop(experiment_id=experiment_id, approved=True, ctx=ctx))
    else:  # pragma: no cover - requires Celery/Redis
        from ..celery_app import resume_experiment_task

        resume_experiment_task.delay(experiment_id, True)
    return {"experiment_id": experiment_id, "status": "resumed"}


@router.get("/{experiment_id}/final", response_model=FinalResultOut)
def get_final(experiment_id: str) -> FinalResultOut:
    with get_session() as s:
        fr = repo.get_final_result(s, experiment_id)
        if fr is None:
            raise HTTPException(404, "no final result yet")
        return FinalResultOut(
            experiment_id=fr.experiment_id,
            attempt=fr.attempt or 0,
            commit_sha=fr.commit_sha,
            winning_run_id=fr.winning_run_id,
            optimal_params=fr.optimal_params or {},
            metrics=fr.metrics or None,
            stop_reason=fr.stop_reason,
            report_md_uri=fr.report_md_uri,
            notified=fr.notified,
            validation=fr.validation,
            changed_params=fr.changed_params or {},
            promotion=fr.promotion,
        )


@router.get("/{experiment_id}/iterations")
def iterations(experiment_id: str, attempt: int | None = None) -> list[dict]:
    with get_session() as s:
        e = repo.get_experiment(s, experiment_id)
        if e is None:
            raise HTTPException(404, "experiment not found")
        att = e.attempt if attempt is None else attempt
        return [
            {"iteration": it.iteration_index, "attempt": it.attempt, "pm_action": it.pm_action,
             "stop_reason": it.stop_reason, "analyst_findings": it.analyst_findings,
             "rationale": it.rationale, "run_id": it.proposed_run_id, "created_at": repo.iso(it.created_at)}
            for it in repo.list_iterations(s, experiment_id, att)
        ]


@router.post("/{experiment_id}/promote", dependencies=[Depends(require_api)])
async def promote(experiment_id: str, body: PromoteIn | None = None,
                  ctx: MasterContext = Depends(get_ctx)) -> dict:
    """Propose the winning parameters as a git branch commit (+ push / PR when configured)."""
    record = await promote_experiment(ctx, experiment_id, body or PromoteIn())
    if not record.get("ok"):
        raise HTTPException(409, record.get("error") or "promotion failed")
    return record
