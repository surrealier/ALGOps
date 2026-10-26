"""Benchmark verdicts: absolute gates first, then comparison against a baseline.

* **gate_failed** — a declared gate (``recall >= 0.85``) is not met (or the metric is missing).
* **regressed**   — every gate passes, but a tracked metric moved in its *worse* direction by
  more than its tolerance ``max(abs_tolerance[m], rel_tolerance * |baseline|)``.
* **improved**    — gates pass, nothing regressed, and some tracked metric got better beyond
  tolerance.
* **passed**      — otherwise; **error** when the run itself failed.

Pure functions — shared by the master's benchmark service and the local ``algops test``.
"""

from __future__ import annotations

from typing import Any

from algops.core import AlgorithmSpec, Constraint, GoalDirection, Verdict, resolve_direction


def evaluate_gates(gates: list[Constraint], metrics: dict[str, float]) -> list[dict[str, Any]]:
    out = []
    for g in gates:
        actual = metrics.get(g.metric)
        out.append({
            "metric": g.metric, "op": g.op, "value": g.value,
            "actual": actual, "passed": g.satisfied(metrics),
        })
    return out


def compare_to_baseline(
    algo: AlgorithmSpec,
    metrics: dict[str, float],
    baseline: dict[str, float] | None,
    *,
    rel_tolerance: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Per tracked metric: {current, baseline, delta, tolerance, direction, status}."""
    if not baseline:
        return {}
    declared = algo.metric_specs()
    policy = algo.regression
    rel = policy.rel_tolerance if rel_tolerance is None else rel_tolerance
    out: dict[str, dict[str, Any]] = {}
    for name in algo.regression_metrics():
        cur, base = metrics.get(name), baseline.get(name)
        direction = resolve_direction(name, declared)
        if cur is None or base is None:
            out[name] = {"current": cur, "baseline": base, "delta": None, "tolerance": None,
                         "direction": direction.value, "status": "missing"}
            continue
        tol = max(float(policy.abs_tolerance.get(name, 0.0)), rel * abs(base))
        delta = cur - base
        worse = delta > tol if direction is GoalDirection.minimize else delta < -tol
        better = delta < -tol if direction is GoalDirection.minimize else delta > tol
        out[name] = {
            "current": cur, "baseline": base, "delta": delta, "tolerance": tol,
            "direction": direction.value,
            "status": "regressed" if worse else ("improved" if better else "same"),
        }
    return out


def decide(run_ok: bool, gates: list[dict[str, Any]], deltas: dict[str, dict[str, Any]]) -> Verdict:
    if not run_ok:
        return Verdict.error
    if any(not g["passed"] for g in gates):
        return Verdict.gate_failed
    statuses = {d["status"] for d in deltas.values()}
    if "regressed" in statuses:
        return Verdict.regressed
    if "improved" in statuses:
        return Verdict.improved
    return Verdict.passed


def judge(
    algo: AlgorithmSpec,
    run_ok: bool,
    metrics: dict[str, float] | None,
    baseline: dict[str, float] | None,
    *,
    rel_tolerance: float | None = None,
) -> tuple[Verdict, list[dict[str, Any]], dict[str, dict[str, Any]]]:
    metrics = metrics or {}
    gates = evaluate_gates(algo.gates, metrics) if run_ok else []
    deltas = compare_to_baseline(algo, metrics, baseline, rel_tolerance=rel_tolerance) if run_ok else {}
    return decide(run_ok, gates, deltas), gates, deltas
