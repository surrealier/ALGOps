"""Wire + state schemas shared by master, slave, agents, and the UI client.

This module is the single source of truth for the master<->slave REST protocol and for the
structured objects the agent loop passes around. Both services import it; the frontend
TypeScript client mirrors the master OpenAPI (which is built from these models).

Contract v2 generalizes the run protocol: a run evaluates *one algorithm* of *one repository at
one commit* on *one suite* with one parameter set, and reports arbitrary named metrics.
"""

from __future__ import annotations

import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

from .enums import CommandKind, GoalDirection, MetricName, PMAction, RunStatus, StopReason
from .metrics import Constraint, derive_binary, normalize_metrics
from .params import ParamChange, ParamSpec

# --------------------------------------------------------------------------- metrics


class Metrics(BaseModel):
    """Binary-preset helper: confusion-matrix-derived metrics for one run.

    Kept for adapters that report counts. On the wire, metrics are a plain ``{name: float}``
    dict (``RunResult.metrics``) so any algorithm can report any metric.
    """

    tp: int = 0
    fp: int = 0
    fn: int = 0
    tn: int = 0
    precision: float = 0.0
    recall: float = 0.0
    f1: float = 0.0
    accuracy: float = 0.0
    fp_rate: float = 0.0
    fn_rate: float = 0.0

    @classmethod
    def from_counts(cls, tp: int, fp: int, fn: int, tn: int) -> "Metrics":
        d = derive_binary({"tp": tp, "fp": fp, "fn": fn, "tn": tn})
        return cls(
            tp=tp, fp=fp, fn=fn, tn=tn,
            precision=d["precision"], recall=d["recall"], f1=d["f1"], accuracy=d["accuracy"],
            fp_rate=d["fp_rate"], fn_rate=d["fn_rate"],
        )

    def get(self, metric: MetricName | str) -> float:
        name = metric.value if isinstance(metric, MetricName) else str(metric)
        return float(getattr(self, name))


# --------------------------------------------------------------------------- repo refs

_REF_BAD = re.compile(r"[\s\x00-\x1f\x7f~^:?*\[\\]|\.\.|@\{|//")
_URL_BAD = re.compile(r"[\s\x00-\x1f\x7f]")


def check_git_ref(value: str | None) -> str | None:
    """A branch / tag / sha that is safe to pass to git (never an option, no revision syntax)."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("-") or value.startswith("/") or value.endswith((".", "/", ".lock")) \
            or _REF_BAD.search(value) or len(value) > 255:
        raise ValueError(f"invalid git ref {value!r}")
    return value


def check_git_url(value: str | None) -> str | None:
    """A clone URL: no leading '-' (never an option), no whitespace/control characters."""
    if value is None:
        return None
    value = value.strip()
    if not value:
        return None
    if value.startswith("-") or _URL_BAD.search(value) or len(value) > 2048:
        raise ValueError(f"invalid git url {value!r}")
    if value.lower().startswith("ext::") or value.lower().startswith("fd::"):
        raise ValueError(f"git transport not allowed: {value!r}")
    return value


class RepoRef(BaseModel):
    """Where a project's code lives. ``git``: cloned by the runner; ``local``: a path on the
    serving runner's host (dev fast-loop, legacy single-project slaves)."""

    kind: Literal["git", "local"] = "git"
    name: str = ""  # project name (local projects are matched by name on the runner)
    url: str | None = None  # git url (git) — local projects keep their path runner-side
    ref: str | None = None  # branch / tag / sha to resolve (None => remote default branch)
    sha: str | None = None  # resolved commit (runs are always pinned to a sha)

    @field_validator("url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return check_git_url(v)

    @field_validator("ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return check_git_ref(v)


# --------------------------------------------------------------------------- run protocol


class RunRequest(BaseModel):
    """Master -> slave: evaluate one parameter set of one algorithm on one suite.

    The first five fields are the v1 contract (``run(req, output_dir)`` adapters receive this
    object); everything after them is v2 and optional so old adapters keep working.
    """

    run_id: str
    experiment_id: str = ""
    project_id: str
    param_values: dict[str, Any] = Field(default_factory=dict)
    test_set_uri: str = ""
    # --- v2 ---
    benchmark_id: str | None = None
    repo: RepoRef | None = None
    algorithm_id: str = "default"
    suite_id: str = "default"
    kind: Literal["search", "validation", "benchmark", "adhoc"] = "search"
    seed: int = 0
    timeout_s: float | None = None


class RunResult(BaseModel):
    """Slave -> master: the outcome of evaluating one parameter set."""

    run_id: str
    status: RunStatus
    metrics: dict[str, float] | None = None
    counts: dict[str, int] | None = None
    csv_manifest_uri: str | None = None
    n_items: int | None = None
    error: str | None = None
    # --- v2 ---
    error_kind: Literal["program", "protocol", "timeout", "infra", "setup", "params"] | None = None
    commit_sha: str | None = None
    duration_s: float | None = None
    log_uri: str | None = None
    param_values: dict[str, Any] | None = None  # the *effective* params the program saw
    extra: dict[str, Any] = Field(default_factory=dict)

    @field_validator("metrics", mode="before")
    @classmethod
    def _norm(cls, v: Any) -> Any:
        return None if v is None else normalize_metrics(v)

    def metric(self, name: str) -> float | None:
        return (self.metrics or {}).get(name)


# --------------------------------------------------------------------------- sync / promote


class SyncRequest(BaseModel):
    """Resolve ``repo.ref`` to a sha and read the manifest at that commit."""

    project_id: str
    repo: RepoRef


class SyncResult(BaseModel):
    ok: bool
    sha: str | None = None
    ref: str | None = None
    dirty: bool = False
    manifest: dict | None = None  # manifest_to_wire(...)
    error: str | None = None


class PullRequestSpec(BaseModel):
    provider: Literal["github", "gitlab"] = "github"
    title: str
    body: str = ""
    base: str | None = None  # target branch (None => repo default)


class PromoteRequest(BaseModel):
    """Propose new parameter defaults as a git branch commit (+ optional push / PR)."""

    project_id: str
    repo: RepoRef  # repo.sha = base commit (the campaign's pinned sha)
    algorithm_id: str
    params: dict[str, Any]  # key -> new default (only the changed ones are written)
    branch: str
    message: str
    push: bool = False  # the PR itself (if any) is opened by the master, not the runner


class PromoteResult(BaseModel):
    ok: bool
    branch: str | None = None
    commit_sha: str | None = None
    base_sha: str | None = None
    changed: dict[str, dict[str, Any]] = Field(default_factory=dict)  # key -> {old, new}
    files: list[str] = Field(default_factory=list)
    patch: str | None = None  # `git format-patch` text — always available
    pushed: bool = False
    pr_url: str | None = None
    error: str | None = None


# --------------------------------------------------------------------------- slave lifecycle


class LocalProjectDecl(BaseModel):
    """A project a runner serves from a local path (no git clone)."""

    name: str
    modality: str = "tabular"
    sha: str | None = None
    dirty: bool = False
    manifest: dict | None = None
    error: str | None = None


class SlaveRegisterRequest(BaseModel):
    enroll_token: str
    host_label: str = "unknown"
    # --- v2 runner capabilities ---
    labels: list[str] = Field(default_factory=list)
    concurrency: int = Field(default=1, ge=1, le=256)
    allow_projects: list[str] | None = None  # project names this runner may serve (None => any)
    git: bool = True  # can clone/checkout git projects
    local_projects: list[LocalProjectDecl] = Field(default_factory=list)
    version: str = ""
    # --- v1 (single local project); still accepted ---
    project_name: str | None = None
    modality: str = "tabular"
    param_spec: ParamSpec | None = None


class SlaveRegisterResponse(BaseModel):
    slave_id: str
    project_id: str = ""  # v1: the (first) local project's id
    slave_token: str  # runner bearer; returned once
    project_ids: dict[str, str] = Field(default_factory=dict)  # local project name -> id


class HeartbeatRequest(BaseModel):
    status: Literal["online", "busy"] = "online"
    inflight: int = 0


# --------------------------------------------------------------------------- command bus
# Slaves never accept inbound connections (NAT/firewall-friendly). The master enqueues a
# command; the slave long-polls for it, executes locally, and POSTs the result back.


class NextCommandResponse(BaseModel):
    kind: CommandKind = CommandKind.noop
    command_id: str | None = None
    run_spec: RunRequest | None = None
    sync_spec: SyncRequest | None = None
    promote_spec: PromoteRequest | None = None


class CommandResultUpload(BaseModel):
    command_id: str
    result: RunResult | None = None
    sync_result: SyncResult | None = None
    promote_result: PromoteResult | None = None


# --------------------------------------------------------------------------- PM decision


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
    repo_url: str | None = None  # git URL => a git project; None => local (created by a runner)
    default_ref: str | None = None  # None => the remote's default branch
    watch: bool = False  # benchmark automatically when the default ref moves

    @field_validator("repo_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return check_git_url(v)

    @field_validator("default_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return check_git_ref(v)


class ProjectUpdate(BaseModel):
    description: str | None = None
    default_ref: str | None = None
    watch: bool | None = None
    repo_url: str | None = None

    @field_validator("repo_url")
    @classmethod
    def _url(cls, v: str | None) -> str | None:
        return check_git_url(v)

    @field_validator("default_ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return check_git_ref(v)


class ProjectOut(BaseModel):
    project_id: str
    name: str
    modality: str
    description: str | None = None
    slave_online: bool = False
    repo_kind: str = "local"
    repo_url: str | None = None
    default_ref: str | None = None
    watch: bool = False
    latest_sha: str | None = None
    synced_at: str | None = None
    algorithms: list[str] = Field(default_factory=list)
    suites: list[str] = Field(default_factory=list)


class RevisionOut(BaseModel):
    revision_id: str
    project_id: str
    ref: str | None
    sha: str
    dirty: bool = False
    synced_at: str
    manifest: dict


class ObjectiveIn(BaseModel):
    metric: str
    direction: GoalDirection | None = None
    target: float | None = None
    constraints: list[Constraint] = Field(default_factory=list)


class ExperimentCreate(BaseModel):
    project_id: str
    name: str = "experiment"
    goal_text: str = ""  # free-text objective, e.g. "오탐 완화"
    # --- v2 ---
    algorithm_id: str | None = None  # None => the project's only/first algorithm
    suite_id: str | None = None  # tuning suite; None => the algorithm's first suite
    validation_suite_ids: list[str] = Field(default_factory=list)  # best params re-checked here
    objective: ObjectiveIn | None = None  # None => legacy target_* fields / manifest default
    strategy: Literal["coordinate", "random", "grid"] = "coordinate"
    search_keys: list[str] | None = None  # None => every param of the algorithm
    batch_size: int = Field(default=1, ge=1, le=64)  # candidates evaluated per iteration
    seed: int = 0
    ref: str | None = None  # None => project default ref

    @field_validator("ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return check_git_ref(v)

    # --- v1 (still accepted) ---
    target_metric: str = MetricName.fp_rate.value
    target_value: float | None = 0.1
    goal_direction: GoalDirection | None = None
    max_iteration: int = Field(default=8, ge=1, le=500)
    primary_param_key: str | None = None  # v1: single-param search => search_keys=[key]
    test_set_uri: str = "bundled://toy"

    @field_validator("target_metric", mode="before")
    @classmethod
    def _tm(cls, v: Any) -> Any:
        return v.value if isinstance(v, MetricName) else v


class ExperimentOut(BaseModel):
    experiment_id: str
    project_id: str
    name: str
    goal_text: str
    target_metric: str
    target_value: float | None
    goal_direction: GoalDirection
    max_iteration: int
    status: str
    best_run_id: str | None = None
    algorithm_id: str = "default"
    suite_id: str = "default"
    validation_suite_ids: list[str] = Field(default_factory=list)
    objective: dict | None = None
    strategy: str = "coordinate"
    search_keys: list[str] | None = None
    batch_size: int = 1
    ref: str | None = None
    commit_sha: str | None = None
    attempt: int = 0
    created_at: str | None = None


class RunOut(BaseModel):
    run_id: str
    experiment_id: str | None
    iteration_index: int
    status: str
    param_values: dict[str, Any]
    metrics: dict[str, float] | None = None
    score: float | None = None
    summary_md_uri: str | None = None
    benchmark_id: str | None = None
    algorithm_id: str = "default"
    suite_id: str = "default"
    commit_sha: str | None = None
    kind: str = "search"
    duration_s: float | None = None
    error: str | None = None
    log_uri: str | None = None
    attempt: int = 0


class FinalResultOut(BaseModel):
    experiment_id: str
    attempt: int = 0
    commit_sha: str | None = None
    winning_run_id: str | None
    optimal_params: dict[str, Any]
    metrics: dict[str, float] | None
    stop_reason: str | None
    report_md_uri: str | None = None
    notified: bool = False
    validation: dict[str, Any] | None = None  # suite_id -> {run_id, metrics, feasible}
    changed_params: dict[str, Any] = Field(default_factory=dict)  # key -> {old, new}
    promotion: dict[str, Any] | None = None


class PromoteIn(BaseModel):
    push: bool | None = None  # None => master setting
    pull_request: bool = False
    title: str | None = None
    base: str | None = None


# --------------------------------------------------------------------------- benchmarks


class BenchmarkScope(BaseModel):
    project_id: str
    ref: str | None = None  # None => project default ref
    algorithms: list[str] | None = None
    suites: list[str] | None = None
    tags: list[str] | None = None

    @field_validator("ref")
    @classmethod
    def _ref(cls, v: str | None) -> str | None:
        return check_git_ref(v)


class BenchmarkCreate(BaseModel):
    name: str = "benchmark"
    scope: list[BenchmarkScope] | None = None  # None => every project
    baseline: Literal["previous", "none"] | str = "previous"  # or a benchmark_id
    trigger: Literal["manual", "schedule", "watch", "api"] = "manual"


class BenchmarkCellOut(BaseModel):
    cell_id: str
    benchmark_id: str
    project_id: str
    project_name: str = ""
    algorithm_id: str
    suite_id: str
    commit_sha: str | None
    run_id: str | None
    verdict: str  # pending | passed | gate_failed | regressed | improved | error
    metrics: dict[str, float] | None = None
    gates: list[dict[str, Any]] = Field(default_factory=list)
    baseline_run_id: str | None = None
    baseline_sha: str | None = None
    deltas: dict[str, dict[str, Any]] = Field(default_factory=dict)
    error: str | None = None
    duration_s: float | None = None


class BenchmarkOut(BaseModel):
    benchmark_id: str
    name: str
    status: str  # created | running | passed | failed | error
    trigger: str
    created_at: str
    finished_at: str | None = None
    scope: list[dict] | None = None
    summary: dict[str, Any] = Field(default_factory=dict)
    revisions: dict[str, str] = Field(default_factory=dict)  # project_id -> sha
    cells: list[BenchmarkCellOut] | None = None


# --------------------------------------------------------------------------- schedules


class ScheduleCreate(BaseModel):
    name: str = "nightly"
    kind: Literal["cron", "watch"] = "cron"
    cron: str | None = "0 2 * * *"  # cron kind (server local time)
    interval_s: int = Field(default=300, ge=30)  # watch kind: poll cadence
    scope: list[BenchmarkScope] | None = None  # None => every project (watch: every watched)
    enabled: bool = True


class ScheduleOut(BaseModel):
    schedule_id: str
    name: str
    kind: str
    cron: str | None
    interval_s: int
    scope: list[dict] | None
    enabled: bool
    last_fired_at: str | None = None
    next_fire_at: str | None = None
    last_benchmark_id: str | None = None


class SlaveOut(BaseModel):
    slave_id: str
    host_label: str
    status: str
    online: bool
    labels: list[str] = Field(default_factory=list)
    concurrency: int = 1
    inflight: int = 0
    git: bool = True
    local_projects: list[str] = Field(default_factory=list)
    allow_projects: list[str] | None = None
    last_heartbeat: str | None = None
    version: str = ""
