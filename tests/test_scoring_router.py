"""Scoring helpers and the loop router's stop conditions (enforced in code, not the agent)."""

from __future__ import annotations

from algops.agents.nodes import route_after_pm
from algops.core import GoalDirection, Metrics
from algops.extraction import is_better, objective_value, target_reached


def test_is_better_directions():
    assert is_better(0.1, None, "minimize")  # first value always better
    assert is_better(0.1, 0.2, GoalDirection.minimize)
    assert not is_better(0.3, 0.2, GoalDirection.minimize)
    assert is_better(0.9, 0.8, GoalDirection.maximize)


def test_target_reached_directions():
    m = Metrics.from_counts(tp=5, fp=1, fn=5, tn=20)  # fp_rate small, recall 0.5
    assert target_reached(m, "fp_rate", 0.1, "minimize")
    assert not target_reached(m, "recall", 0.9, "maximize")
    assert objective_value(m, "fp_rate") == m.fp_rate


def _state(**kw):
    base = dict(
        target_metric="fp_rate", target_value=0.05, goal_direction="minimize",
        max_iteration=10, iteration=1, pm_action="continue",
        last_metrics=Metrics.from_counts(tp=9, fp=9, fn=1, tn=1).model_dump(),  # fp_rate 0.9
    )
    base.update(kw)
    return base


def test_router_continue():
    assert route_after_pm(_state()) == "continue"


def test_router_pm_stop():
    assert route_after_pm(_state(pm_action="stop")) == "finalize"


def test_router_max_iteration():
    assert route_after_pm(_state(iteration=10)) == "finalize"


def test_router_target_reached_overrides_continue():
    good = Metrics.from_counts(tp=9, fp=0, fn=1, tn=10).model_dump()  # fp_rate 0
    assert route_after_pm(_state(last_metrics=good, pm_action="continue")) == "finalize"
