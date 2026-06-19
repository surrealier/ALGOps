"""SQLAlchemy 2.0 ORM models for the master tracking store (plan §3.1, M0 subset).

Hierarchy: Project -< Experiment (campaign == LangGraph thread) -< Run (one param-set
evaluation) ; Iteration is the agent-reasoning step that produced a Run. Metrics are kept
both denormalized on Run (fast read) and in the long ``run_metric`` table (EAV, plan §3.1).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .db import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Project(Base):
    __tablename__ = "project"

    project_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(255))
    modality: Mapped[str] = mapped_column(String(32), default="tabular")
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    param_spec: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    summary_md_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    experiments: Mapped[list["Experiment"]] = relationship(back_populates="project")
    slaves: Mapped[list["Slave"]] = relationship(back_populates="project")


class Slave(Base):
    __tablename__ = "slave"

    slave_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.project_id"))
    token_hash: Mapped[str] = mapped_column(String(64))
    host_label: Mapped[str] = mapped_column(String(255), default="unknown")
    status: Mapped[str] = mapped_column(String(16), default="offline")
    last_heartbeat: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    project: Mapped["Project"] = relationship(back_populates="slaves")


class Experiment(Base):
    __tablename__ = "experiment"

    experiment_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(ForeignKey("project.project_id"))
    name: Mapped[str] = mapped_column(String(255), default="experiment")
    goal_text: Mapped[str] = mapped_column(Text, default="")
    target_metric: Mapped[str] = mapped_column(String(32))
    target_value: Mapped[float] = mapped_column(Float)
    goal_direction: Mapped[str] = mapped_column(String(16))
    max_iteration: Mapped[int] = mapped_column(Integer, default=8)
    primary_param_key: Mapped[str | None] = mapped_column(String(255), nullable=True)
    test_set_uri: Mapped[str] = mapped_column(String(512), default="bundled://toy")
    status: Mapped[str] = mapped_column(String(32), default="created")
    best_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    # The LangGraph checkpoint thread == experiment_id (campaign spans many runs).
    langgraph_thread_id: Mapped[str] = mapped_column(String(36))
    llm_config: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, onupdate=_utcnow
    )

    project: Mapped["Project"] = relationship(back_populates="experiments")
    runs: Mapped[list["Run"]] = relationship(back_populates="experiment")


class Run(Base):
    __tablename__ = "run"

    run_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experiment_id: Mapped[str] = mapped_column(ForeignKey("experiment.experiment_id"))
    project_id: Mapped[str] = mapped_column(String(36))
    iteration_index: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="evaluated")
    param_values: Mapped[dict] = mapped_column(JSON, default=dict)
    # Self-FK mirroring the Neo4j (:Run)-[:DERIVED_FROM]->(:Run) lineage edge.
    derived_from_run: Mapped[str | None] = mapped_column(String(36), nullable=True)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    score: Mapped[float | None] = mapped_column(Float, nullable=True)
    n_items: Mapped[int | None] = mapped_column(Integer, nullable=True)
    csv_manifest_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    summary_md_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)

    experiment: Mapped["Experiment"] = relationship(back_populates="runs")
    run_metrics: Mapped[list["RunMetric"]] = relationship(back_populates="run")


class RunMetric(Base):
    __tablename__ = "run_metric"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("run.run_id"))
    name: Mapped[str] = mapped_column(String(32))
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
    winning_run_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    optimal_params: Mapped[dict] = mapped_column(JSON, default=dict)
    metrics: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    stop_reason: Mapped[str | None] = mapped_column(String(32), nullable=True)
    report_md_uri: Mapped[str | None] = mapped_column(String(512), nullable=True)
    notified: Mapped[bool] = mapped_column(Boolean, default=False)
    notify_refs: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_utcnow)
