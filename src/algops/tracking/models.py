"""SQLAlchemy 2.0 ORM models for the master tracking store (contract v2).

    Project (a repository: git url or a runner-local path)
      -< ProjectRevision   (resolved commit sha + manifest snapshot — algorithms/suites/params)
      -< Experiment        (an optimization campaign on one algorithm; attempt-scoped threads)
           -< Iteration    (agent reasoning step)          -< Run (search / validation)
      -< Benchmark         (a matrix of algorithm × suite runs at pinned shas)
           -< BenchmarkCell (verdict: gates + baseline comparison)    -< Run (benchmark)
    Slave (a runner host: labels, concurrency, local projects)
    Schedule (cron / git-watch triggers)

Metrics are free-form ``{name: float}`` JSON on Run (fast read) and long-form in ``run_metric``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text

# Python None -> SQL NULL (plain JSON stores the JSON literal `null`, which `IS NOT NULL` filters
# would then treat as a value).
NullableJSON = JSON(none_as_null=True)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "project"

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), unique=True)
    modality: Mapped[str] = mapped_column(String(32), default="tabular")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # v1: the single algorithm's param spec (kept in sync with the latest revision's "default").
    param_spec: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    summary_md_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    # v2: where the code lives.
    repo_kind: Mapped[str] = mapped_column(String(16), default="local")  # git | local
    repo_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    default_ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    watch: Mapped[bool] = mapped_column(Boolean, default=False)
    last_seen_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    experiments: Mapped[list["Experiment"]] = relationship(back_populates="project")
    revisions: Mapped[list["ProjectRevision"]] = relationship(back_populates="project")


class ProjectRevision(Base):
    """One synced commit of a project: the manifest (with resolved param specs) at that sha."""

    __tablename__ = "project_revision"
    __table_args__ = (Index("ix_revision_project_synced", "project_id", "synced_at"),)

    revision_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.project_id"))
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sha: Mapped[str] = mapped_column(String(80))
    dirty: Mapped[bool] = mapped_column(Boolean, default=False)
    manifest: Mapped[dict] = mapped_column(JSON)
    synced_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    project: Mapped["Project"] = relationship(back_populates="revisions")


class Slave(Base):
    """A runner host. v1 slaves were bound to one project; v2 runners serve many."""

    __tablename__ = "slave"

    slave_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True)  # v1 binding
    token_hash: Mapped[str] = mapped_column(String(64))
    host_label: Mapped[str] = mapped_column(String(255), default="unknown")
    status: Mapped[str] = mapped_column(String(16), default="offline")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    labels: Mapped[list | None] = mapped_column(NullableJSON, nullable=True)
    concurrency: Mapped[int] = mapped_column(Integer, default=1)
    inflight: Mapped[int] = mapped_column(Integer, default=0)
    git: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_projects: Mapped[list | None] = mapped_column(NullableJSON, nullable=True)
    local_projects: Mapped[list | None] = mapped_column(NullableJSON, nullable=True)  # project names
    version: Mapped[str] = mapped_column(String(64), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Experiment(Base):
    __tablename__ = "experiment"

    experiment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.project_id"))
    name: Mapped[str] = mapped_column(String(255), default="experiment")
    goal_text: Mapped[str] = mapped_column(Text, default="")
    target_metric: Mapped[str] = mapped_column(String(128))
    target_value: Mapped[float | None] = mapped_column(Float, nullable=True)
    goal_direction: Mapped[str] = mapped_column(String(16))
    max_iteration: Mapped[int] = mapped_column(Integer, default=8)
    primary_param_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    test_set_uri: Mapped[str] = mapped_column(String(512), default="")
    status: Mapped[str] = mapped_column(String(32), default="created")
    best_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # LangGraph thread = f"{experiment_id}:{attempt}" — a re-run starts a clean thread.
    langgraph_thread_id: Mapped[str] = mapped_column(String(80))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    llm_config: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    # v2
    algorithm_id: Mapped[str] = mapped_column(String(64), default="default")
    suite_id: Mapped[str] = mapped_column(String(64), default="default")
    validation_suite_ids: Mapped[list | None] = mapped_column(NullableJSON, nullable=True)
    objective: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    strategy: Mapped[str] = mapped_column(String(32), default="coordinate")
    search_keys: Mapped[list | None] = mapped_column(NullableJSON, nullable=True)
    batch_size: Mapped[int] = mapped_column(Integer, default=1)
    seed: Mapped[int] = mapped_column(Integer, default=0)
    ref: Mapped[str | None] = mapped_column(String(255), nullable=True)
    commit_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    project: Mapped["Project"] = relationship(back_populates="experiments")
    runs: Mapped[list["Run"]] = relationship(back_populates="experiment")


class Run(Base):
    __tablename__ = "run"
    __table_args__ = (
        Index("ix_run_experiment", "experiment_id", "attempt", "iteration_index"),
        Index("ix_run_cell", "project_id", "algorithm_id", "suite_id", "created_at"),
        Index("ix_run_benchmark", "benchmark_id"),
    )

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str | None] = mapped_column(ForeignKey("experiment.experiment_id"), nullable=True)
    benchmark_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    project_id: Mapped[str] = mapped_column(String(36))
    algorithm_id: Mapped[str] = mapped_column(String(64), default="default")
    suite_id: Mapped[str] = mapped_column(String(64), default="default")
    commit_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    kind: Mapped[str] = mapped_column(String(16), default="search")  # search|validation|benchmark
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    iteration_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="evaluated")
    param_values: Mapped[dict] = mapped_column(JSON, default=dict)
    # Self-FK mirroring the Neo4j (:Run)-[:DERIVED_FROM]->(:Run) lineage edge.
    derived_from_run: Mapped[str | None] = mapped_column(String(36), nullable=True)
    metrics: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    csv_manifest_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary_md_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    log_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    slave_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_kind: Mapped[str | None] = mapped_column(String(16), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    experiment: Mapped["Experiment | None"] = relationship(back_populates="runs")
    run_metrics: Mapped[list["RunMetric"]] = relationship(back_populates="run")


class RunMetric(Base):
    __tablename__ = "run_metric"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.run_id"))
    name: Mapped[str] = mapped_column(String(128))
    value: Mapped[float] = mapped_column(Float)

    run: Mapped["Run"] = relationship(back_populates="run_metrics")


class RunParam(Base):
    __tablename__ = "run_param"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.run_id"))
    key: Mapped[str] = mapped_column(String(255))
    value: Mapped[str] = mapped_column(String(250))


class Iteration(Base):
    __tablename__ = "iteration"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiment.experiment_id"))
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    iteration_index: Mapped[int] = mapped_column(Integer)
    analyst_findings: Mapped[str | None] = mapped_column(Text, nullable=True)
    pm_action: Mapped[str | None] = mapped_column(String(16), nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    rationale: Mapped[str | None] = mapped_column(Text, nullable=True)
    proposed_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class FinalResult(Base):
    __tablename__ = "final_result"

    experiment_id: Mapped[str] = mapped_column(
        ForeignKey("experiment.experiment_id"), primary_key=True
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    commit_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)  # what the winner ran on
    winning_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    optimal_params: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    report_md_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_refs: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    validation: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    changed_params: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    promotion: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)


class Benchmark(Base):
    __tablename__ = "benchmark"

    benchmark_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="benchmark")
    status: Mapped[str] = mapped_column(String(16), default="created")
    trigger: Mapped[str] = mapped_column(String(16), default="manual")
    scope: Mapped[list | None] = mapped_column(NullableJSON, nullable=True)
    baseline: Mapped[str] = mapped_column(String(64), default="previous")
    revisions: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)  # project_id -> sha
    summary: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    schedule_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    cells: Mapped[list["BenchmarkCell"]] = relationship(back_populates="benchmark")


class BenchmarkCell(Base):
    __tablename__ = "benchmark_cell"
    __table_args__ = (Index("ix_cell_key", "project_id", "algorithm_id", "suite_id"),)

    cell_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    benchmark_id: Mapped[str] = mapped_column(ForeignKey("benchmark.benchmark_id"))
    project_id: Mapped[str] = mapped_column(String(36))
    algorithm_id: Mapped[str] = mapped_column(String(64))
    suite_id: Mapped[str] = mapped_column(String(64))
    commit_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    verdict: Mapped[str] = mapped_column(String(16), default="pending")
    metrics: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    gates: Mapped[list | None] = mapped_column(NullableJSON, nullable=True)
    baseline_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    baseline_sha: Mapped[str | None] = mapped_column(String(80), nullable=True)
    deltas: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    duration_s: Mapped[float | None] = mapped_column(Float, nullable=True)
    # A human accepted this cell as the new baseline (e.g. an intentional trade-off).
    accepted: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    benchmark: Mapped["Benchmark"] = relationship(back_populates="cells")


class Schedule(Base):
    __tablename__ = "schedule"

    schedule_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), default="nightly")
    kind: Mapped[str] = mapped_column(String(16), default="cron")  # cron | watch
    cron: Mapped[str | None] = mapped_column(String(128), nullable=True)
    interval_s: Mapped[int] = mapped_column(Integer, default=300)
    scope: Mapped[list | None] = mapped_column(NullableJSON, nullable=True)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    last_fired_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_fire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_benchmark_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_seen: Mapped[dict | None] = mapped_column(NullableJSON, nullable=True)  # watch: project_id -> sha
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
