"""Toy run-adapter (M2 multi-stage): monotonic tradeoff, determinism, manifest, sensitivity."""

from __future__ import annotations

import json
import os

from algops.core import RunRequest, new_id
from algops.extraction import aggregate_multistage, load_manifest, resolve_sibling_uri
from algops.toy_project import load_param_spec, run as toy_run

PK = "detector.confidence_threshold"
RK = "postproc.roundness.min"


def _run(tmp_path, params):
    req = RunRequest(
        run_id=new_id(), experiment_id="e", project_id="p",
        param_values=params, test_set_uri="bundled://toy",
    )
    return toy_run(req, os.path.join(str(tmp_path), "out_" + new_id()[:8]))


def _read(path):
    return open(path, encoding="utf-8").read()


def test_param_spec_has_two_params():
    spec = load_param_spec()
    keys = [p.key for p in spec.params]
    assert PK in keys and RK in keys
    assert spec.defaults()[RK] == 0.0  # roundness filter disabled by default (back-compat)


def test_monotonic_tradeoff(tmp_path):
    lo = _run(tmp_path, {PK: 0.05}).metrics
    hi = _run(tmp_path, {PK: 0.85}).metrics
    assert lo.fp_rate > hi.fp_rate
    assert lo.fn_rate < hi.fn_rate


def test_determinism(tmp_path):
    a = _run(tmp_path, {PK: 0.5}).metrics
    b = _run(tmp_path, {PK: 0.5}).metrics
    assert a.model_dump() == b.model_dump()


def test_manifest_and_multistage_aggregation(tmp_path):
    res = _run(tmp_path, {PK: 0.45})
    assert res.n_items == 280
    assert res.csv_manifest_uri.endswith("manifest.json")
    manifest = load_manifest(_read(res.csv_manifest_uri))
    assert manifest is not None
    stages = {s["stage"] for s in manifest["stages"]}
    assert stages == {"ai_inference", "postproc_shape", "postproc_temporal", "decision"}

    stage_texts = {
        s["stage"]: _read(resolve_sibling_uri(res.csv_manifest_uri, s["csv"]))
        for s in manifest["stages"]
    }
    assert stage_texts["ai_inference"].splitlines()[0].startswith(
        "run_id,item_id,frame_index,entity_id,stage,ai_confidence"
    )
    agg = aggregate_multistage(manifest, stage_texts)
    # Master-side recompute (joined on entity_id) must match the slave-reported metrics.
    assert agg["metrics"].model_dump() == res.metrics.model_dump()
    assert agg["stage_counts"]["decision"] == 280


def test_roundness_param_attributed_in_sensitivity(tmp_path):
    # Low threshold (keeps most by confidence) + high roundness min -> drops attributed to roundness.
    res = _run(tmp_path, {PK: 0.05, RK: 0.7})
    manifest = load_manifest(_read(res.csv_manifest_uri))
    stage_texts = {
        s["stage"]: _read(resolve_sibling_uri(res.csv_manifest_uri, s["csv"]))
        for s in manifest["stages"]
    }
    agg = aggregate_multistage(manifest, stage_texts)
    assert agg["sensitivity"].get(RK, 0) > 0  # roundness filter is responsible for drops
    # With the default (0.0) the roundness filter drops nothing.
    res0 = _run(tmp_path, {PK: 0.05, RK: 0.0})
    m0 = load_manifest(_read(res0.csv_manifest_uri))
    st0 = {s["stage"]: _read(resolve_sibling_uri(res0.csv_manifest_uri, s["csv"])) for s in m0["stages"]}
    assert aggregate_multistage(m0, st0)["sensitivity"].get(RK, 0) == 0
