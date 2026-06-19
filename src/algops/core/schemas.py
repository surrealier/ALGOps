"""Wire + state schemas shared by master, slave, agents, and the UI client.

This module is the single source of truth for the master<->slave REST protocol and for
the structured objects the agent loop passes around. Both services import it; the frontend
TypeScript client is generated from the master OpenAPI (which is built from these models).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from .enums import CommandKind, GoalDirection, MetricName, PMAction, RunStatus, StopReason
from .params import ParamChange, ParamSpec

# --------------------------------------------------------------------------- metrics


class Metrics(BaseModel):
    """Confusion-matrix-derived evaluation metrics for one run over the test set."""

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    fp_rate: float = 0.0
    fn_rate: float = 0.0

    @classmethod
    def from_counts(cls, tp: int, fp: int, fn: int, tn: int) -> "Metrics":
        precision = tp / (tp + fp) if (tp + fp) else 0.0
        recall = tp / (tp + fn) if (tp + fn) else 0.0
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        fp_rate = fp / (fp + tn) if (fp + tn) else 0.0
        fn_rate = fn / (fn + tp) if (fn + tp) else 0.0
        return cls(
            tp=tp, fp=fp, fn=fn, tn=tn,
            precision=precision, recall=recall, f1=f1,
            fp_rate=fp_rate, fn_rate=fn_rate,
        )

    def get(self, metric: MetricName | str) -> float:
        return float(getattr(self, MetricName(metric).value))


# --------------------------------------------------------------------------- run protocol


class RunRequest(BaseModel):
    """Master -> slave: evaluate one parameter set over the test set (plan §2C)."""

    run_id: str
    experiment_id: str
    project_id: str
    param_values: dict[str, Any]
    test_set_uri: str


class RunResult(BaseModel):
    """Slave -> master: the outcome of evaluating one parameter set."""

    run_id: str
    status: RunStatus
    metrics: Metrics | None = None
    csv_manifest_uri: str | None = None
    n_items: int | None = None
    error: str | None = None


# --------------------------------------------------------------------------- slave lifecycle


class SlaveRegisterRequest(BaseModel):
    enroll_token: str
    project_name: str
    modality: str = "tabular"
    host_label: str = "unknown"
    param_spec: ParamSpec | None = None


class SlaveRegisterResponse(BaseModel):
    slave_id: str
    project_id: str
    slave_token: str  # project-scoped bearer; returned once


class HeartbeatRequest(BaseModel):
    status: Literal["online", "busy"] = "online"


# --------------------------------------------------------------------------- command bus
# Slaves never accept inbound connections (NAT/firewall-friendly). The master enqueues a
# command; the slave long-polls for it, executes locally, and POSTs the result back.


class NextCommandResponse(BaseModel):
    kind: CommandKind = CommandKind.noop
    command_id: str | None = None
    run_spec: RunRequest | None = None


class CommandResultUpload(BaseModel):
    command_id: str
    result: RunResult


# --------------------------------------------------------------------------- PM decision
# Produced deterministically in M0; from M1 emitted by the PM agent via structured output.


class PMDecision(BaseModel):
    action: PMAction
    next_params: dict[str, Any] | None = None
    param_change: ParamChange | None = None
    stop_reason: StopReason | None = None
    rationale: str = ""
    iteration: int = 0


# --------------------------------------------------------------------------- API DTOs (UI)


class ProjectCreate(BaseModel):
    name: str
    modality: str = "tabular"
    description: str | None = None


class ProjectOut(BaseModel):
    project_id: str
    name: str
    modality: str
    description: str | None = None
    slave_online: bool = False


class ExperimentCreate(BaseModel):
    project_id: str
    name: str = "experiment"
    goal_text: str = ""  # free-text objective, e.g. "오탐 완화"
    target_metric: MetricName = MetricName.fp_rate
    target_value: float = 0.1
    goal_direction: GoalDirection | None = None  # inferred from target_metric if omitted
    max_iteration: int = Field(default=8, ge=1, le=200)
    primary_param_key: str | None = None  # M0 deterministic search targets this key
    test_set_uri: str = "bundled://toy"

    def resolved_direction(self) -> GoalDirection:
        return self.goal_direction or GoalDirection.for_metric(self.target_metric)


class ExperimentOut(BaseModel):
    experiment_id: str
    project_id: str
    name: str
    goal_text: str
    target_metric: MetricName
    target_value: float
    goal_direction: GoalDirection
    max_iteration: int
    status: str
    best_run_id: str | None = None


class RunOut(BaseModel):
    run_id: str
    experiment_id: str
    iteration_index: int
    status: str
    param_values: dict[str, Any]
    metrics: Metrics | None = None
    score: float | None = None
    summary_md_uri: str | None = None


class FinalResultOut(BaseModel):
    experiment_id: str
    winning_run_id: str | None
    optimal_params: dict[str, Any]
    metrics: Metrics | None
    stop_reason: str | None
    report_md_uri: str | None = None
    notified: bool = False
