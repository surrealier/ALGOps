"""Objective scoring and target-reached checks — enforced in code, never by the agent.

The user's free-text goal maps to a ``target_metric`` (e.g. ``fp_rate``) with a direction
(minimize/maximize). ``objective_value`` is the scalar the loop optimizes; ``target_reached``
is one of the loop's stop conditions (checked in the router, plan §4).
"""

from __future__ import annotations

import math

from algops.core import GoalDirection, Metrics, MetricName


def objective_value(metrics: Metrics, target_metric: MetricName | str) -> float:
    """The scalar to optimize for this experiment (the raw target-metric value)."""
    return metrics.get(target_metric)


def is_better(candidate: float, incumbent: float | None, direction: GoalDirection | str) -> bool:
    """Is ``candidate`` a strictly better objective value than the incumbent best?"""
    if incumbent is None or math.isnan(incumbent):
        return True
    direction = GoalDirection(direction)
    if direction is GoalDirection.minimize:
        return candidate < incumbent
    return candidate > incumbent


def target_reached(
    metrics: Metrics,
    target_metric: MetricName | str,
    target_value: float,
    direction: GoalDirection | str,
) -> bool:
    value = metrics.get(target_metric)
    direction = GoalDirection(direction)
    if direction is GoalDirection.minimize:
        return value <= target_value
    return value >= target_value
