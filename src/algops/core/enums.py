"""Shared enumerations (string-valued so they serialize cleanly over REST/JSON)."""

from __future__ import annotations

from enum import Enum


class Modality(str, Enum):
    video = "video"
    image = "image"
    audio = "audio"
    tabular = "tabular"


class Dtype(str, Enum):
    float = "float"
    int = "int"
    bool = "bool"
    enum = "enum"


class Influence(str, Enum):
    high = "high"
    medium = "medium"
    low = "low"
    none = "none"
    unknown = "unknown"


class MetricName(str, Enum):
    fp = "fp"
    fn = "fn"
    tp = "tp"
    tn = "tn"
    precision = "precision"
    recall = "recall"
    f1 = "f1"
    fp_rate = "fp_rate"
    fn_rate = "fn_rate"


# Lower is better for these; higher is better for the rest.
_MINIMIZE = {MetricName.fp, MetricName.fn, MetricName.fp_rate, MetricName.fn_rate}


class GoalDirection(str, Enum):
    minimize = "minimize"
    maximize = "maximize"

    @classmethod
    def for_metric(cls, metric: "MetricName") -> "GoalDirection":
        return cls.minimize if metric in _MINIMIZE else cls.maximize


class ExperimentStatus(str, Enum):
    created = "created"
    running = "running"
    paused = "paused"
    stopped_by_pm = "stopped_by_pm"
    stopped_max_iter = "stopped_max_iter"
    target_reached = "target_reached"
    failed = "failed"


TERMINAL_STATUSES = frozenset(
    {
        ExperimentStatus.stopped_by_pm,
        ExperimentStatus.stopped_max_iter,
        ExperimentStatus.target_reached,
        ExperimentStatus.failed,
    }
)


class StopReason(str, Enum):
    target_reached = "target_reached"
    max_iteration = "max_iteration"
    pm_stop = "pm_stop"
    converged = "converged"
    error = "error"


class PMAction(str, Enum):
    continue_ = "continue"
    stop = "stop"


class SlaveStatus(str, Enum):
    online = "online"
    offline = "offline"
    busy = "busy"


class CommandKind(str, Enum):
    run = "run"
    noop = "noop"


class RunStatus(str, Enum):
    evaluated = "evaluated"
    failed = "failed"
