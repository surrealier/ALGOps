"""Repository functions over the tracking models (MLflow-like data access).

All functions take an explicit Session so callers control the transaction boundary
(``with get_session() as s: ...``). Token verification uses a salted-free sha256 of the
opaque bearer (fine for M0; rotate to a real secret store in M5).
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from algops.core import (
    ExperimentCreate,
    Metrics,
    ProjectCreate,
    new_id,
    new_token,
)
from algops.core.enums import ExperimentStatus, SlaveStatus

from .models import (
    Experiment,
    FinalResult,
    Iteration,
    Project,
    Run,
    RunMetric,
    RunParam,
    Slave,
)

HEARTBEAT_TIMEOUT_S = 30.0


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- projects


def create_project(s: Session, body: ProjectCreate, param_spec: dict | None = None) -> Project:
    proj = Project(
        project_id=new_id(),
        name=body.name,
        modality=body.modality,
        description=body.description,
        param_spec=param_spec,
    )
    s.add(proj)
    s.flush()
    return proj


def get_project(s: Session, project_id: str) -> Project | None:
    return s.get(Project, project_id)


def list_projects(s: Session) -> list[Project]:
    return list(s.scalars(select(Project).order_by(Project.created_at.desc())))


# --------------------------------------------------------------------------- slaves


def register_slave(
    s: Session, project_id: str, host_label: str, param_spec: dict | None = None
) -> tuple[Slave, str]:
    token = new_token()
    slave = Slave(
        slave_id=new_id(),
        project_id=project_id,
        token_hash=hash_token(token),
        host_label=host_label,
        status=SlaveStatus.online.value,
        last_heartbeat=datetime.now(timezone.utc),
    )
    s.add(slave)
    if param_spec is not None:
        proj = s.get(Project, project_id)
        if proj is not None:
            proj.param_spec = param_spec
    s.flush()
    return slave, token


def get_slave(s: Session, slave_id: str) -> Slave | None:
    return s.get(Slave, slave_id)


def verify_slave_token(s: Session, slave_id: str, token: str) -> bool:
    slave = s.get(Slave, slave_id)
    return slave is not None and slave.token_hash == hash_token(token)


def touch_heartbeat(s: Session, slave_id: str, status: str = "online") -> None:
    slave = s.get(Slave, slave_id)
    if slave is not None:
        slave.last_heartbeat = datetime.now(timezone.utc)
        slave.status = status


def online_slave_for_project(s: Session, project_id: str) -> Slave | None:
    cutoff = datetime.now(timezone.utc).timestamp() - HEARTBEAT_TIMEOUT_S
    rows = s.scalars(
        select(Slave).where(Slave.project_id == project_id).order_by(Slave.last_heartbeat.desc())
    )
    for slave in rows:
        hb = slave.last_heartbeat
        if hb is not None and hb.replace(tzinfo=timezone.utc).timestamp() >= cutoff:
            return slave
    return None


# --------------------------------------------------------------------------- experiments


def create_experiment(s: Session, body: ExperimentCreate) -> Experiment:
    exp_id = new_id()
    exp = Experiment(
        experiment_id=exp_id,
        project_id=body.project_id,
        name=body.name,
        goal_text=body.goal_text,
        target_metric=body.target_metric.value,
        target_value=body.target_value,
        goal_direction=body.resolved_direction().value,
        max_iteration=body.max_iteration,
        primary_param_key=body.primary_param_key,
        test_set_uri=body.test_set_uri,
        status=ExperimentStatus.created.value,
        langgraph_thread_id=exp_id,  # campaign thread == experiment_id
    )
    s.add(exp)
    s.flush()
    return exp


def get_experiment(s: Session, experiment_id: str) -> Experiment | None:
    return s.get(Experiment, experiment_id)


def list_experiments(s: Session, project_id: str | None = None) -> list[Experiment]:
    stmt = select(Experiment).order_by(Experiment.created_at.desc())
    if project_id:
        stmt = stmt.where(Experiment.project_id == project_id)
    return list(s.scalars(stmt))


def set_experiment_status(s: Session, experiment_id: str, status: str) -> None:
    exp = s.get(Experiment, experiment_id)
    if exp is not None:
        exp.status = status


def set_best_run(s: Session, experiment_id: str, run_id: str) -> None:
    exp = s.get(Experiment, experiment_id)
    if exp is not None:
        exp.best_run_id = run_id


# --------------------------------------------------------------------------- runs


def create_run(
    s: Session,
    run_id: str,
    experiment_id: str,
    project_id: str,
    iteration_index: int,
    param_values: dict,
    derived_from_run: str | None = None,
) -> Run:
    run = Run(
        run_id=run_id,
        experiment_id=experiment_id,
        project_id=project_id,
        iteration_index=iteration_index,
        param_values=param_values,
        derived_from_run=derived_from_run,
        status="pending",
    )
    s.add(run)
    for key, value in param_values.items():
        s.add(RunParam(run_id=run_id, key=key, value=str(value)[:250]))
    s.flush()
    return run


def record_run_result(
    s: Session,
    run_id: str,
    status: str,
    metrics: Metrics | None,
    score: float | None,
    n_items: int | None,
    csv_manifest_uri: str | None,
    summary_md_uri: str | None = None,
) -> None:
    run = s.get(Run, run_id)
    if run is None:
        return
    run.status = status
    run.score = score
    run.n_items = n_items
    run.csv_manifest_uri = csv_manifest_uri
    if summary_md_uri:
        run.summary_md_uri = summary_md_uri
    if metrics is not None:
        run.metrics = metrics.model_dump()
        for name, value in metrics.model_dump().items():
            s.add(RunMetric(run_id=run_id, name=name, value=float(value)))


def get_run(s: Session, run_id: str) -> Run | None:
    return s.get(Run, run_id)


def list_runs(s: Session, experiment_id: str) -> list[Run]:
    return list(
        s.scalars(
            select(Run)
            .where(Run.experiment_id == experiment_id)
            .order_by(Run.iteration_index.asc())
        )
    )


# --------------------------------------------------------------------------- iterations


def create_iteration(
    s: Session,
    experiment_id: str,
    iteration_index: int,
    analyst_findings: str | None,
    pm_action: str | None,
    stop_reason: str | None,
    rationale: str | None,
    proposed_run_id: str | None,
) -> Iteration:
    it = Iteration(
        experiment_id=experiment_id,
        iteration_index=iteration_index,
        analyst_findings=analyst_findings,
        pm_action=pm_action,
        stop_reason=stop_reason,
        rationale=rationale,
        proposed_run_id=proposed_run_id,
    )
    s.add(it)
    s.flush()
    return it


# --------------------------------------------------------------------------- final result


def upsert_final_result(
    s: Session,
    experiment_id: str,
    winning_run_id: str | None,
    optimal_params: dict,
    metrics: Metrics | None,
    stop_reason: str | None,
    report_md_uri: str | None,
    notified: bool = False,
    notify_refs: dict | None = None,
) -> FinalResult:
    fr = s.get(FinalResult, experiment_id)
    if fr is None:
        fr = FinalResult(experiment_id=experiment_id)
        s.add(fr)
    fr.winning_run_id = winning_run_id
    fr.optimal_params = optimal_params
    fr.metrics = metrics.model_dump() if metrics else None
    fr.stop_reason = stop_reason
    fr.report_md_uri = report_md_uri
    fr.notified = notified
    fr.notify_refs = notify_refs
    s.flush()
    return fr


def get_final_result(s: Session, experiment_id: str) -> FinalResult | None:
    return s.get(FinalResult, experiment_id)
