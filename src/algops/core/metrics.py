"""Declarative, algorithm-agnostic metrics, objectives and constraints (contract v2).

Algorithms report *any* named scalar metrics (``{"recall": 0.91, "latency_ms": 12.3}``) and may
also report confusion counts (``tp/fp/fn/tn``) from which the ``binary`` preset derives the
classic rates. Each algorithm *declares* the metrics it cares about with a direction
(minimize / maximize), so the loop, the benchmark verdicts and the UI never hardcode a metric.

An :class:`Objective` is what a campaign optimizes: one metric + direction + optional target,
subject to constraints (e.g. *minimize fp_rate while recall >= 0.9*). Feasible candidates always
beat infeasible ones; among infeasible ones, the smaller total constraint violation wins.
"""

from __future__ import annotations

import math
import re
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .enums import GoalDirection

METRIC_NAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.@:/-]{0,127}$")

Op = Literal[">=", "<=", ">", "<", "=="]


class MetricSpec(BaseModel):
    """A metric an algorithm declares: its name, which way is better, and presentation hints."""

    model_config = ConfigDict(extra="forbid")

    name: str
    direction: GoalDirection
    unit: str | None = None
    description: str | None = None

    @field_validator("name")
    @classmethod
    def _name(cls, v: str) -> str:
        if not METRIC_NAME_PATTERN.match(v):
            raise ValueError(f"invalid metric name {v!r}")
        return v


# --------------------------------------------------------------------------- presets

_MIN = GoalDirection.minimize
_MAX = GoalDirection.maximize

PRESETS: dict[str, list[MetricSpec]] = {
    # Binary detection / classification (post-processing FP/FN work).
    "binary": [
        MetricSpec(name="tp", direction=_MAX, description="true positives"),
        MetricSpec(name="fp", direction=_MIN, description="false positives (오탐)"),
        MetricSpec(name="fn", direction=_MIN, description="false negatives (미탐)"),
        MetricSpec(name="tn", direction=_MAX, description="true negatives"),
        MetricSpec(name="precision", direction=_MAX),
        MetricSpec(name="recall", direction=_MAX),
        MetricSpec(name="f1", direction=_MAX),
        MetricSpec(name="accuracy", direction=_MAX),
        MetricSpec(name="fp_rate", direction=_MIN, description="오탐율 fp/(fp+tn)"),
        MetricSpec(name="fn_rate", direction=_MIN, description="미탐율 fn/(fn+tp)"),
    ],
    # Regression / estimation error.
    "regression": [
        MetricSpec(name="mae", direction=_MIN),
        MetricSpec(name="rmse", direction=_MIN),
        MetricSpec(name="mse", direction=_MIN),
        MetricSpec(name="max_abs_error", direction=_MIN),
    ],
    # Runtime cost.
    "latency": [
        MetricSpec(name="latency_ms", direction=_MIN, unit="ms"),
        MetricSpec(name="latency_p95_ms", direction=_MIN, unit="ms"),
        MetricSpec(name="throughput", direction=_MAX, unit="items/s"),
    ],
}

# Token heuristics for metrics an algorithm reports but did not declare (display / sane default
# only — declare the metric in algops.yaml when the name is ambiguous).
_MIN_TOKENS = {"loss", "error", "err", "mae", "mse", "rmse", "mape", "latency", "time", "ms",
               "sec", "seconds", "fp", "fn", "fpr", "fnr", "eer", "miss", "misses", "false",
               "cost", "drift", "wer", "cer"}
_MAX_TOKENS = {"acc", "accuracy", "precision", "recall", "f1", "map", "iou", "miou", "auc",
               "score", "tp", "tn", "tpr", "tnr", "throughput", "fps", "hit", "hits", "dice",
               "psnr", "ssim", "bleu"}
_MAX_PREFIXES = ("map", "iou", "auc", "f1")


def preset_specs(names: list[str]) -> list[MetricSpec]:
    out: list[MetricSpec] = []
    for name in names:
        if name not in PRESETS:
            raise ValueError(f"unknown metric preset {name!r} (known: {sorted(PRESETS)})")
        out.extend(PRESETS[name])
    return out


def known_direction(name: str) -> GoalDirection | None:
    for specs in PRESETS.values():
        for spec in specs:
            if spec.name == name:
                return spec.direction
    tokens = [t for t in re.split(r"[^a-z0-9]+", name.lower()) if t]
    if any(t in _MAX_TOKENS for t in tokens):
        return _MAX
    if any(t in _MIN_TOKENS for t in tokens):
        return _MIN
    if tokens and tokens[0].startswith(_MAX_PREFIXES):
        return _MAX
    return None


def resolve_direction(name: str, declared: dict[str, MetricSpec] | None = None) -> GoalDirection:
    """Declared direction first, then preset/name heuristics; default maximize."""
    if declared and name in declared:
        return declared[name].direction
    return known_direction(name) or _MAX


# --------------------------------------------------------------------------- values


def derive_binary(counts: dict[str, Any]) -> dict[str, float]:
    tp = int(counts.get("tp", 0))
    fp = int(counts.get("fp", 0))
    fn = int(counts.get("fn", 0))
    tn = int(counts.get("tn", 0))
    precision = tp / (tp + fp) if (tp + fp) else 0.0
    recall = tp / (tp + fn) if (tp + fn) else 0.0
    f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
    total = tp + fp + fn + tn
    return {
        "tp": float(tp), "fp": float(fp), "fn": float(fn), "tn": float(tn),
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "accuracy": (tp + tn) / total if total else 0.0,
        "fp_rate": fp / (fp + tn) if (fp + tn) else 0.0,
        "fn_rate": fn / (fn + tp) if (fn + tp) else 0.0,
    }


def normalize_metrics(raw: Any) -> dict[str, float]:
    """Coerce a metrics payload (dict / pydantic model) into ``{name: finite float}``.

    Non-numeric and non-finite values are dropped (they cannot be compared or JSON-encoded).
    """
    if raw is None:
        return {}
    if isinstance(raw, BaseModel):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise TypeError(f"metrics must be a mapping, got {type(raw).__name__}")
    out: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or value is None:
            continue
        try:
            f = float(value)
        except (TypeError, ValueError):
            continue
        if math.isfinite(f):
            out[str(key)] = f
    return out


def complete_metrics(metrics: dict[str, float], counts: dict[str, Any] | None) -> dict[str, float]:
    """Fill preset-derived values from counts without overriding explicitly reported values."""
    if not counts:
        return dict(metrics)
    derived = derive_binary(counts)
    merged = dict(derived)
    merged.update(metrics)
    return merged


# --------------------------------------------------------------------------- objectives


def _compare(value: float, op: Op, ref: float) -> bool:
    if op == ">=":
        return value >= ref
    if op == "<=":
        return value <= ref
    if op == ">":
        return value > ref
    if op == "<":
        return value < ref
    return math.isclose(value, ref, rel_tol=1e-9, abs_tol=1e-12)


class Constraint(BaseModel):
    """``metric op value`` — e.g. ``recall >= 0.9``. Also used for absolute benchmark gates."""

    model_config = ConfigDict(extra="forbid")

    metric: str
    op: Op = ">="
    value: float

    def satisfied(self, metrics: dict[str, float]) -> bool:
        v = metrics.get(self.metric)
        return v is not None and _compare(v, self.op, self.value)

    def violation(self, metrics: dict[str, float]) -> float:
        """0 when satisfied; otherwise how far off (missing metric => +inf)."""
        v = metrics.get(self.metric)
        if v is None:
            return math.inf
        if self.satisfied(metrics):
            return 0.0
        return abs(v - self.value) or 1e-12

    def describe(self) -> str:
        return f"{self.metric} {self.op} {self.value:g}"


class Objective(BaseModel):
    metric: str
    direction: GoalDirection
    target: float | None = None
    constraints: list[Constraint] = Field(default_factory=list)

    def value(self, metrics: dict[str, float]) -> float | None:
        return metrics.get(self.metric)

    def violation(self, metrics: dict[str, float]) -> float:
        return sum(c.violation(metrics) for c in self.constraints)

    def feasible(self, metrics: dict[str, float]) -> bool:
        return all(c.satisfied(metrics) for c in self.constraints)

    def rank(self, metrics: dict[str, float] | None) -> tuple[int, float, float]:
        """Sort key — lower is better: (infeasible?, violation, signed objective)."""
        if not metrics:
            return (2, math.inf, math.inf)
        v = self.value(metrics)
        if v is None:
            return (2, math.inf, math.inf)
        viol = self.violation(metrics)
        signed = v if self.direction is GoalDirection.minimize else -v
        return (0 if viol == 0 else 1, viol, signed)

    def better(self, candidate: dict[str, float] | None, incumbent: dict[str, float] | None) -> bool:
        """Strictly better than the incumbent (None incumbent => any evaluated candidate wins)."""
        if incumbent is None:
            return bool(candidate) and self.value(candidate) is not None
        return self.rank(candidate) < self.rank(incumbent)

    def reached(self, metrics: dict[str, float] | None) -> bool:
        if not metrics or self.target is None or not self.feasible(metrics):
            return False
        v = self.value(metrics)
        if v is None:
            return False
        if self.direction is GoalDirection.minimize:
            return v <= self.target
        return v >= self.target

    def describe(self) -> str:
        arrow = "≤" if self.direction is GoalDirection.minimize else "≥"
        head = f"{self.metric} {self.direction.value}"
        if self.target is not None:
            head += f" (목표 {arrow} {self.target:g})"
        if self.constraints:
            head += " s.t. " + ", ".join(c.describe() for c in self.constraints)
        return head
