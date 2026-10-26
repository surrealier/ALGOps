"""Contract v2: manifest parsing, legacy synthesis, metrics/objectives, param coercion, verdicts."""

from __future__ import annotations

import os

import pytest

from algops.core import (
    Constraint,
    GoalDirection,
    ManifestError,
    Objective,
    ParamSpec,
    Verdict,
    complete_metrics,
    load_manifest,
    manifest_from_wire,
    manifest_to_wire,
    parse_manifest,
    resolve_direction,
)
from algops.extraction.verdict import judge

from .conftest import EXAMPLES

PY = os.path.join(EXAMPLES, "py-multi-algo")


def _algo(**kw):
    base = {"id": "a", "entrypoint": {"command": ["true"]}}
    base.update(kw)
    return base


def test_example_manifest_resolves_both_param_styles():
    m = load_manifest(PY)
    assert [a.id for a in m.algorithms] == ["detector", "smoother"]
    det, smo = m.algorithm("detector"), m.algorithm("smoother")
    assert {p.key for p in det.spec().params} == {"detector.score_threshold", "detector.min_area", "detector.area_rule"}
    assert smo.spec().defaults() == {"smoother.alpha": 0.9, "smoother.window": 1}  # inline params
    assert [s.id for s in m.suites_for("detector")] == ["det-smoke", "det-full"]
    assert m.cells(tags=["fast"]) == [("detector", "det-smoke"), ("smoother", "sig-smoke")]
    # Wire round-trip keeps the resolved specs (the master never needs the repo files).
    again = manifest_from_wire(manifest_to_wire(m))
    assert again.algorithm("detector").spec().defaults() == det.spec().defaults()


def test_manifest_validation_errors():
    with pytest.raises(ManifestError, match="unknown suite"):
        parse_manifest({"algorithms": [_algo(suites=["nope"])], "suites": [{"id": "s"}]})
    with pytest.raises(ManifestError, match="duplicate algorithm"):
        parse_manifest({"algorithms": [_algo(), _algo()]})
    with pytest.raises(ManifestError, match="exactly one"):
        parse_manifest({"algorithms": [{"id": "a", "entrypoint": {}}]})
    with pytest.raises(ManifestError, match="preset"):
        parse_manifest({"algorithms": [_algo(metrics={"presets": ["nonsense"]})]})
    m = parse_manifest({"algorithms": [_algo()]})
    assert [s.id for s in m.suites] == ["default"]  # a suite is synthesized when none declared


def test_legacy_layout_is_synthesized(tmp_path):
    (tmp_path / "algops").mkdir()
    (tmp_path / "algops" / "params.yaml").write_text(
        "meta: {algops_contract_version: '1.0', modality: video}\n"
        "params:\n  - {key: a.b, dtype: float, default: 0.5, range: [0, 1], step: 0.1, stage: decision}\n",
        encoding="utf-8")
    (tmp_path / "algops" / "run_adapter.py").write_text("def run(req, out):\n    pass\n", encoding="utf-8")
    m = load_manifest(str(tmp_path))
    assert m.legacy and m.algorithms[0].id == "default" and m.algorithms[0].modality == "video"
    assert m.algorithms[0].entrypoint.python == "algops/run_adapter.py:run"
    assert "fp_rate" in m.algorithms[0].metric_specs()


def test_metric_directions_and_derivation():
    assert resolve_direction("fp_rate") is GoalDirection.minimize
    assert resolve_direction("mAP@50") is GoalDirection.maximize
    assert resolve_direction("latency_p95_ms") is GoalDirection.minimize
    assert resolve_direction("fps") is GoalDirection.maximize
    m = complete_metrics({"latency_ms": 3.0, "precision": 0.99}, {"tp": 8, "fp": 2, "fn": 2, "tn": 8})
    assert m["recall"] == 0.8 and m["latency_ms"] == 3.0
    assert m["precision"] == 0.99  # an explicitly reported value is never overridden


def test_objective_feasibility_first():
    obj = Objective(metric="fp_rate", direction=GoalDirection.minimize, target=0.05,
                    constraints=[Constraint(metric="recall", op=">=", value=0.8)])
    infeasible_great = {"fp_rate": 0.0, "recall": 0.5}
    feasible_ok = {"fp_rate": 0.1, "recall": 0.85}
    assert obj.better(feasible_ok, infeasible_great)
    assert not obj.reached({"fp_rate": 0.01, "recall": 0.7})
    assert obj.reached({"fp_rate": 0.04, "recall": 0.9})
    assert obj.rank(None)[0] == 2  # a failed run ranks last


def test_param_coercion_and_grid():
    spec = ParamSpec(params=[
        {"key": "x.t", "dtype": "float", "default": 0.5, "range": [0, 1], "step": 0.25, "stage": "s"},
        {"key": "x.n", "dtype": "int", "default": 3, "range": [1, 5], "step": 2, "stage": "s"},
        {"key": "x.m", "dtype": "enum", "default": "a", "enum": ["a", "b"], "stage": "s"},
        {"key": "x.b", "dtype": "bool", "default": False, "stage": "s"},
    ])
    eff = spec.effective({"x.t": 7, "x.n": 4.6, "x.b": "true"})
    assert eff == {"x.t": 1.0, "x.n": 5, "x.m": "a", "x.b": True}
    with pytest.raises(ValueError, match="unknown parameter"):
        spec.effective({"x.typo": 1})
    with pytest.raises(ValueError):
        spec.effective({"x.m": "z"})
    assert spec.by_key()["x.t"].grid() == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert spec.by_key()["x.n"].grid() == [1, 3, 5]


def test_verdicts_gate_then_baseline():
    m = load_manifest(PY)
    det = m.algorithm("detector")
    good = {"recall": 0.9, "precision": 0.7, "fp_rate": 0.30}
    v, gates, deltas = judge(det, True, good, None)
    assert v is Verdict.passed and all(g["passed"] for g in gates) and deltas == {}
    v, gates, _ = judge(det, True, {**good, "recall": 0.5}, None)
    assert v is Verdict.gate_failed and not next(g for g in gates if g["metric"] == "recall")["passed"]
    v, _, deltas = judge(det, True, {**good, "fp_rate": 0.40}, good)
    assert v is Verdict.regressed and deltas["fp_rate"]["status"] == "regressed"
    v, _, _ = judge(det, True, {**good, "fp_rate": 0.305}, good)  # within 3% tolerance
    assert v is Verdict.passed
    v, _, _ = judge(det, True, {**good, "fp_rate": 0.20}, good)
    assert v is Verdict.improved
    v, _, _ = judge(det, False, None, good)
    assert v is Verdict.error


def test_shipped_manifest_schema_is_current():
    from algops.contract.schema import BUNDLE, render

    with open(os.path.join(BUNDLE, "algops.schema.json"), encoding="utf-8") as fh:
        assert fh.read() == render(), "run: python -m algops.contract.schema"
