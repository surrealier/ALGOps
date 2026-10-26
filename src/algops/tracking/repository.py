"""Repository functions over the tracking models (MLflow-like data access).

All functions take an explicit Session so callers control the transaction boundary
(``with get_session() as s: ...``). Bearer tokens are stored as sha256 digests only.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import Session

from algops.core import (
    ProjectCreate,
    RunResult,
    Verdict,
    manifest_from_wire,
    new_id,
    new_token,
    normalize_metrics,
)
from algops.core.enums import BenchmarkStatus, ExperimentStatus, SlaveStatus

from .models import (
    Benchmark,
    BenchmarkCell,
    Experiment,
    FinalResult,
    Iteration,
    Project,
    ProjectRevision,
    Run,
    RunMetric,
    RunParam,
    Schedule,
    Slave,
)

HEARTBEAT_TIMEOUT_S = 30.0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def aware(dt: datetime | None) -> datetime | None:
    """SQLite drops tzinfo; treat naive datetimes as UTC."""
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def iso(dt: datetime | None) -> str | None:
    dt = aware(dt)
    return dt.isoformat() if dt else None


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------- projects


def create_project(s: Session, body: ProjectCreate, param_spec: dict | None = None,
                   repo_kind: str | None = None) -> Project:
    proj = Project(
        project_id=new_id(),
        name=body.name,
        modality=body.modality,
        description=body.description,
        param_spec=param_spec,
        repo_kind=repo_kind or ("git" if body.repo_url else "local"),
        repo_url=body.repo_url,
        default_ref=body.default_ref,
        watch=bool(body.watch),
    )
    s.add(proj)
    s.flush()
    return proj


def get_project(s: Session, project_id: str) -> Project | None:
    return s.get(Project, project_id)


def get_project_by_name(s: Session, name: str) -> Project | None:
    return s.scalars(select(Project).where(Project.name == name)).first()


def find_project(s: Session, ident: str) -> Project | None:
    return get_project(s, ident) or get_project_by_name(s, ident)


def list_projects(s: Session) -> list[Project]:
    return list(s.scalars(select(Project).order_by(Project.created_at.desc())))


def add_revision(s: Session, project_id: str, *, ref: str | None, sha: str, dirty: bool,
                 manifest: dict) -> ProjectRevision:
    rev = ProjectRevision(revision_id=new_id(), project_id=project_id, ref=ref, sha=sha,
                          dirty=dirty, manifest=manifest, synced_at=utcnow())
    s.add(rev)
    proj = s.get(Project, project_id)
    if proj is not None:
        try:
            m = manifest_from_wire(manifest)
            algo = next((a for a in m.algorithms if a.id == "default"), m.algorithms[0])
            proj.param_spec = algo.spec().model_dump(mode="json")
            proj.modality = algo.modality
        except Exception:  # noqa: BLE001 — a bad manifest must not block recording the sync
            pass
    s.flush()
    return rev


def latest_revision(s: Session, project_id: str, sha: str | None = None) -> ProjectRevision | None:
    stmt = select(ProjectRevision).where(ProjectRevision.project_id == project_id)
    if sha:
        stmt = stmt.where(ProjectRevision.sha == sha)
    return s.scalars(stmt.order_by(ProjectRevision.synced_at.desc())).first()


def list_revisions(s: Session, project_id: str, limit: int = 20) -> list[ProjectRevision]:
    return list(s.scalars(
        select(ProjectRevision).where(ProjectRevision.project_id == project_id)
        .order_by(ProjectRevision.synced_at.desc()).limit(limit)
    ))


# --------------------------------------------------------------------------- slaves


def register_slave(
    s: Session,
    project_id: str | None = None,
    host_label: str = "unknown",
    param_spec: dict | None = None,
    *,
    labels: list[str] | None = None,
    concurrency: int = 1,
    allow_projects: list[str] | None = None,
    git: bool = True,
    local_projects: list[str] | None = None,
    version: str = "",
) -> tuple[Slave, str]:
    token = new_token()
    slave = Slave(
        slave_id=new_id(),
        project_id=project_id,
        token_hash=hash_token(token),
        host_label=host_label,
        status=SlaveStatus.online.value,
        last_heartbeat=utcnow(),
        labels=list(labels or []),
        concurrency=max(1, int(concurrency)),
        allow_projects=allow_projects,
        git=git,
        local_projects=list(local_projects or []),
        version=version,
    )
    s.add(slave)
    if param_spec is not None and project_id:
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


def touch_heartbeat(s: Session, slave_id: str, status: str = "online", inflight: int | None = None) -> None:
    slave = s.get(Slave, slave_id)
    if slave is not None:
        slave.last_heartbeat = utcnow()
        slave.status = status
        if inflight is not None:
            slave.inflight = int(inflight)


def is_online(slave: Slave, timeout_s: float = HEARTBEAT_TIMEOUT_S) -> bool:
    hb = aware(slave.last_heartbeat)
    return hb is not None and (utcnow() - hb).total_seconds() <= timeout_s


def list_slaves(s: Session) -> list[Slave]:
    return list(s.scalars(select(Slave).order_by(Slave.last_heartbeat.desc())))


def online_slaves(s: Session) -> list[Slave]:
    return [sl for sl in list_slaves(s) if is_online(sl)]


def can_serve(slave: Slave, project: Project, runs_on: list[str] | None = None) -> bool:
    """Dispatch rule: project kind + allowlist + required labels."""
    if project.repo_kind == "local":
        if project.name not in (slave.local_projects or []) and slave.project_id != project.project_id:
            return False
    else:
        if not slave.git:
            return False
    if slave.allow_projects is not None and project.name not in slave.allow_projects \
            and project.project_id not in slave.allow_projects:
        return False
    need = set(runs_on or [])
    return need.issubset(set(slave.labels or []))


def eligible_slaves(s: Session, project: Project, runs_on: list[str] | None = None) -> list[Slave]:
    return [sl for sl in online_slaves(s) if can_serve(sl, project, runs_on)]


def online_slave_for_project(s: Session, project_id: str) -> Slave | None:
    proj = s.get(Project, project_id)
    if proj is None:
        return None
    slaves = eligible_slaves(s, proj)
    return slaves[0] if slaves else None


# --------------------------------------------------------------------------- experiments


def create_experiment(s: Session, **fields: Any) -> Experiment:
    exp_id = new_id()
    exp = Experiment(
        experiment_id=exp_id,
        status=ExperimentStatus.created.value,
        langgraph_thread_id=f"{exp_id}:0",
        attempt=0,
        **fields,
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


def set_experiment_status(s: Session, experiment_id: str, status: str, error: str | None = None) -> None:
    exp = s.get(Experiment, experiment_id)
    if exp is not None:
        exp.status = status
        if error is not None or status != ExperimentStatus.failed.value:
            exp.error = error


def start_attempt(s: Session, experiment_id: str) -> Experiment | None:
    """A fresh (re-)run: bump the attempt so the LangGraph thread starts clean."""
    exp = s.get(Experiment, experiment_id)
    if exp is None:
        return None
    if exp.status != ExperimentStatus.created.value or exp.best_run_id is not None:
        exp.attempt = int(exp.attempt or 0) + 1
    exp.langgraph_thread_id = f"{exp.experiment_id}:{exp.attempt}"
    exp.best_run_id = None
    exp.error = None
    return exp


def set_best_run(s: Session, experiment_id: str, run_id: str) -> None:
    exp = s.get(Experiment, experiment_id)
    if exp is not None:
        exp.best_run_id = run_id


# --------------------------------------------------------------------------- runs


def create_run(
    s: Session,
    run_id: str,
    experiment_id: str | None,
    project_id: str,
    iteration_index: int = 0,
    param_values: dict | None = None,
    derived_from_run: str | None = None,
    *,
    benchmark_id: str | None = None,
    algorithm_id: str = "default",
    suite_id: str = "default",
    commit_sha: str | None = None,
    kind: str = "search",
    attempt: int = 0,
) -> Run:
    run = Run(
        run_id=run_id,
        experiment_id=experiment_id,
        benchmark_id=benchmark_id,
        project_id=project_id,
        algorithm_id=algorithm_id,
        suite_id=suite_id,
        commit_sha=commit_sha,
        kind=kind,
        attempt=attempt,
        iteration_index=iteration_index,
        param_values=dict(param_values or {}),
        derived_from_run=derived_from_run,
        status="pending",
    )
    s.add(run)
    for key, value in (param_values or {}).items():
        s.add(RunParam(run_id=run_id, key=key, value=str(value)[:250]))
    s.flush()
    return run


def record_run_result(
    s: Session,
    run_id: str,
    status: str,
    metrics: Any,
    score: float | None,
    n_items: int | None,
    csv_manifest_uri: str | None,
    summary_md_uri: str | None = None,
    *,
    result: RunResult | None = None,
    slave_id: str | None = None,
    log_uri: str | None = None,
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
    if log_uri:
        run.log_uri = log_uri
    if slave_id:
        run.slave_id = slave_id
    if result is not None:
        run.duration_s = result.duration_s
        run.error = result.error
        run.error_kind = result.error_kind
        if result.commit_sha:
            run.commit_sha = result.commit_sha
        if result.param_values:
            run.param_values = dict(result.param_values)
    values = normalize_metrics(metrics)
    if values:
        run.metrics = values
        for name, value in values.items():
            s.add(RunMetric(run_id=run_id, name=name[:128], value=float(value)))


def get_run(s: Session, run_id: str) -> Run | None:
    return s.get(Run, run_id)


def list_runs(s: Session, experiment_id: str, attempt: int | None = None) -> list[Run]:
    stmt = select(Run).where(Run.experiment_id == experiment_id)
    if attempt is not None:
        stmt = stmt.where(Run.attempt == attempt)
    return list(s.scalars(stmt.order_by(Run.attempt.asc(), Run.iteration_index.asc(), Run.created_at.asc())))


def list_cell_history(s: Session, project_id: str, algorithm_id: str, suite_id: str,
                      limit: int = 50) -> list[Run]:
    return list(s.scalars(
        select(Run).where(and_(Run.project_id == project_id, Run.algorithm_id == algorithm_id,
                               Run.suite_id == suite_id, Run.kind == "benchmark",
                               Run.status == "evaluated"))
        .order_by(Run.created_at.desc()).limit(limit)
    ))


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
    attempt: int = 0,
) -> Iteration:
    it = Iteration(
        experiment_id=experiment_id,
        attempt=attempt,
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


def list_iterations(s: Session, experiment_id: str, attempt: int | None = None) -> list[Iteration]:
    stmt = select(Iteration).where(Iteration.experiment_id == experiment_id)
    if attempt is not None:
        stmt = stmt.where(Iteration.attempt == attempt)
    return list(s.scalars(stmt.order_by(Iteration.attempt.asc(), Iteration.iteration_index.asc())))


# --------------------------------------------------------------------------- final result


def upsert_final_result(
    s: Session,
    experiment_id: str,
    winning_run_id: str | None,
    optimal_params: dict,
    metrics: Any,
    stop_reason: str | None,
    report_md_uri: str | None,
    notified: bool = False,
    notify_refs: dict | None = None,
    *,
    attempt: int = 0,
    validation: dict | None = None,
    changed_params: dict | None = None,
    commit_sha: str | None = None,
) -> FinalResult:
    fr = s.get(FinalResult, experiment_id)
    if fr is None:
        fr = FinalResult(experiment_id=experiment_id)
        s.add(fr)
    fr.attempt = attempt
    fr.commit_sha = commit_sha
    fr.winning_run_id = winning_run_id
    fr.optimal_params = optimal_params
    values = normalize_metrics(metrics)
    fr.metrics = values or None
    fr.stop_reason = stop_reason
    fr.report_md_uri = report_md_uri
    fr.notified = notified
    fr.notify_refs = notify_refs
    fr.validation = validation
    fr.changed_params = changed_params
    fr.promotion = None
    s.flush()
    return fr


def get_final_result(s: Session, experiment_id: str) -> FinalResult | None:
    return s.get(FinalResult, experiment_id)


def set_promotion(s: Session, experiment_id: str, promotion: dict) -> None:
    fr = s.get(FinalResult, experiment_id)
    if fr is not None:
        fr.promotion = promotion


# --------------------------------------------------------------------------- benchmarks


def create_benchmark(s: Session, *, name: str, trigger: str, scope: list | None, baseline: str,
                     schedule_id: str | None = None) -> Benchmark:
    b = Benchmark(benchmark_id=new_id(), name=name, trigger=trigger, scope=scope,
                  baseline=baseline, status="created", schedule_id=schedule_id)
    s.add(b)
    s.flush()
    return b


def get_benchmark(s: Session, benchmark_id: str) -> Benchmark | None:
    return s.get(Benchmark, benchmark_id)


def list_benchmarks(s: Session, limit: int = 50, project_id: str | None = None) -> list[Benchmark]:
    stmt = select(Benchmark).order_by(Benchmark.created_at.desc())
    if project_id:
        involved = select(BenchmarkCell.benchmark_id).where(BenchmarkCell.project_id == project_id).distinct()
        stmt = stmt.where(Benchmark.benchmark_id.in_(involved))
    return list(s.scalars(stmt.limit(limit)))


def add_cell(s: Session, *, benchmark_id: str, project_id: str, algorithm_id: str, suite_id: str,
             commit_sha: str | None) -> BenchmarkCell:
    c = BenchmarkCell(cell_id=new_id(), benchmark_id=benchmark_id, project_id=project_id,
                      algorithm_id=algorithm_id, suite_id=suite_id, commit_sha=commit_sha,
                      verdict=Verdict.pending.value)
    s.add(c)
    s.flush()
    return c


def get_cell(s: Session, cell_id: str) -> BenchmarkCell | None:
    return s.get(BenchmarkCell, cell_id)


def list_cells(s: Session, benchmark_id: str) -> list[BenchmarkCell]:
    return list(s.scalars(
        select(BenchmarkCell).where(BenchmarkCell.benchmark_id == benchmark_id)
        .order_by(BenchmarkCell.project_id, BenchmarkCell.algorithm_id, BenchmarkCell.suite_id)
    ))


_BASELINE_VERDICTS = (Verdict.passed.value, Verdict.improved.value)


def baseline_cell(s: Session, *, project_id: str, algorithm_id: str, suite_id: str,
                  exclude_benchmark_id: str, baseline: str = "previous") -> BenchmarkCell | None:
    """The cell this one is compared against.

    ``previous`` => the most recent *accepted* evaluation of the same (project, algorithm,
    suite): a passed/improved cell, or one a human explicitly accepted. A regression therefore
    keeps being reported until it is fixed or accepted. Otherwise ``baseline`` is a benchmark id.
    """
    if baseline == "none":
        return None
    stmt = select(BenchmarkCell).where(and_(
        BenchmarkCell.project_id == project_id,
        BenchmarkCell.algorithm_id == algorithm_id,
        BenchmarkCell.suite_id == suite_id,
        BenchmarkCell.benchmark_id != exclude_benchmark_id,
        BenchmarkCell.metrics.is_not(None),
    ))
    if baseline != "previous":  # an explicit benchmark: its evaluated cells, whatever the verdict
        stmt = stmt.where(BenchmarkCell.benchmark_id == baseline,
                          BenchmarkCell.verdict != Verdict.error.value)
    else:
        stmt = stmt.where(or_(BenchmarkCell.verdict.in_(_BASELINE_VERDICTS), BenchmarkCell.accepted.is_(True)))
    return s.scalars(stmt.order_by(BenchmarkCell.created_at.desc())).first()


# --------------------------------------------------------------------------- schedules


def create_schedule(s: Session, **fields: Any) -> Schedule:
    sch = Schedule(schedule_id=new_id(), **fields)
    s.add(sch)
    s.flush()
    return sch


def get_schedule(s: Session, schedule_id: str) -> Schedule | None:
    return s.get(Schedule, schedule_id)


def list_schedules(s: Session) -> list[Schedule]:
    return list(s.scalars(select(Schedule).order_by(Schedule.created_at.asc())))


def delete_schedule(s: Session, schedule_id: str) -> bool:
    sch = s.get(Schedule, schedule_id)
    if sch is None:
        return False
    s.delete(sch)
    return True


# --------------------------------------------------------------------------- startup reconcile


def reconcile_orphans(s: Session, reason: str = "the master restarted while this was running") -> dict[str, int]:
    """Single-process (dev_mode) startup: workflows ran in the previous process and died with it.

    Running campaigns -> failed (re-run starts a clean attempt; paused ones resume from their
    checkpoint and are left alone); running/created benchmarks -> error, pending cells -> error.
    """
    now = utcnow()
    exps = list(s.scalars(select(Experiment).where(Experiment.status == ExperimentStatus.running.value)))
    for e in exps:
        e.status = ExperimentStatus.failed.value
        e.error = reason
    benches = list(s.scalars(select(Benchmark).where(
        Benchmark.status.in_((BenchmarkStatus.created.value, BenchmarkStatus.running.value)))))
    cells = 0
    for b in benches:
        b.status = BenchmarkStatus.error.value
        b.error = reason
        b.finished_at = b.finished_at or now
        for c in s.scalars(select(BenchmarkCell).where(and_(
                BenchmarkCell.benchmark_id == b.benchmark_id,
                BenchmarkCell.verdict == Verdict.pending.value))):
            c.verdict = Verdict.error.value
            c.error = reason
            cells += 1
    return {"experiments": len(exps), "benchmarks": len(benches), "cells": cells}
