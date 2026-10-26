"""Objective scoring and target-reached checks — enforced in code, never by the agent.

The scalar helpers here are the v1 API (one metric + direction). Campaigns in v2 use
:class:`algops.core.Objective`, which adds constraints (feasibility first) on top of the same
semantics. Both accept any ``{name: float}`` metrics mapping (or a ``Metrics`` model).
"""

from __future__ import annotations

import math
from typing import Any

from algops.core import GoalDirection, MetricName, normalize_metrics


def _metric_name(metric: MetricName | str) -> str:
    return metric.value if isinstance(metric, MetricName) else str(metric)


def objective_value(metrics: Any, target_metric: MetricName | str) -> float:
    """The raw target-metric value (NaN when the run did not report it)."""
    value = normalize_metrics(metrics).get(_metric_name(target_metric))
    return float("nan") if value is None else float(value)


def is_better(candidate: float, incumbent: float | None, direction: GoalDirection | str) -> bool:
    """Is ``candidate`` a strictly better objective value than the incumbent best?"""
    if candidate is None or (isinstance(candidate, float) and math.isnan(candidate)):
        return False
    if incumbent is None or math.isnan(incumbent):
        return True
    direction = GoalDirection(direction)
    if direction is GoalDirection.minimize:
        return candidate < incumbent
    return candidate > incumbent


def target_reached(
    metrics: Any,
    target_metric: MetricName | str,
    target_value: float | None,
    direction: GoalDirection | str,
) -> bool:
    if target_value is None:
        return False
    value = normalize_metrics(metrics).get(_metric_name(target_metric))
    if value is None:
        return False
    direction = GoalDirection(direction)
    if direction is GoalDirection.minimize:
        return value <= target_value
    return value >= target_value
