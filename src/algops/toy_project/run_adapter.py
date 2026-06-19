"""Toy project run-adapter (plan §2C / §3.2, M2 multi-stage).

Synthetic detector + post-processing pipeline, fully deterministic and CPU-only:

    ai_inference   -> per detection: ai_confidence, ai_class, gt_match
    postproc_shape -> per entity:    feat_roundness  (shape feature)
    postproc_temporal -> per entity: dfeat_conf_delta_n3 (windowed feature; telemetry only)
    decision       -> dec_kept / dec_reason / dec_score, joined on entity_id

Two externalized parameters:
  * detector.confidence_threshold  — keep iff ai_confidence >= threshold
  * postproc.roundness.min         — keep iff feat_roundness >= min   (default 0.0 => no-op,
                                       so M0/M1 single-parameter behavior is preserved)
Raising either cuts false positives but adds false negatives, with different fp/fn balance.
"""

from __future__ import annotations

import csv
import json
import os
import random
from dataclasses import dataclass

from algops.core import Metrics, RunRequest, RunResult, RunStatus

from .config import load_params


@dataclass(frozen=True)
class _Candidate:
    item_id: str
    frame_index: int
    det: int
    confidence: float
    roundness: float
    gt_match: int  # 1 = real target, 0 = distractor


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def _build_fixture(n_targets: int = 140, n_distractors: int = 140, seed: int = 42) -> list[_Candidate]:
    rng = random.Random(seed)
    cands: list[_Candidate] = []
    idx = 0
    for _ in range(n_targets):  # targets: higher confidence AND rounder
        frame = idx // 4
        cands.append(
            _Candidate(f"item_{frame:04d}", frame, idx % 4,
                       _clamp01(rng.gauss(0.66, 0.16)), _clamp01(rng.gauss(0.80, 0.12)), 1)
        )
        idx += 1
    for _ in range(n_distractors):  # distractors: lower confidence, less round
        frame = idx // 4
        cands.append(
            _Candidate(f"item_{frame:04d}", frame, idx % 4,
                       _clamp01(rng.gauss(0.36, 0.16)), _clamp01(rng.gauss(0.55, 0.15)), 0)
        )
        idx += 1
    return cands


_FIXTURE = _build_fixture()


def _entity_id(c: _Candidate) -> str:
    return f"{c.item_id}_f{c.frame_index:06d}_d{c.det:02d}"


def _write_csv(path: str, header: list[str], rows: list[list]) -> None:
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(header)
        writer.writerows(rows)


def run(req: RunRequest, output_dir: str) -> RunResult:
    os.makedirs(output_dir, exist_ok=True)
    defaults = load_params()
    threshold = float(
        req.param_values.get("detector.confidence_threshold", defaults.detector_confidence_threshold)
    )
    roundness_min = float(
        req.param_values.get("postproc.roundness.min", defaults.postproc_roundness_min)
    )

    ai_rows, shape_rows, temporal_rows, decision_rows = [], [], [], []
    tp = fp = fn = tn = 0
    for c in _FIXTURE:
        eid = _entity_id(c)
        base = [req.run_id, c.item_id, c.frame_index, eid]
        ai_rows.append(base + ["ai_inference", f"{c.confidence:.4f}", "object", c.gt_match])
        shape_rows.append(base + ["postproc_shape", f"{c.roundness:.4f}"])
        temporal_rows.append(base + ["postproc_temporal", f"{c.confidence - 0.5:.4f}"])

        passes_conf = c.confidence >= threshold
        passes_round = c.roundness >= roundness_min
        kept = 1 if (passes_conf and passes_round) else 0
        if kept:
            reason = "kept"
        elif not passes_conf:
            reason = "below_threshold"
        else:
            reason = "low_roundness"
        decision_rows.append(
            base + ["decision", kept, reason, f"{c.confidence * c.roundness:.4f}", c.gt_match]
        )
        if kept and c.gt_match:
            tp += 1
        elif kept and not c.gt_match:
            fp += 1
        elif not kept and c.gt_match:
            fn += 1
        else:
            tn += 1

    JOIN = ["run_id", "item_id", "frame_index", "entity_id", "stage"]
    _write_csv(os.path.join(output_dir, "ai_inference.csv"),
               JOIN + ["ai_confidence", "ai_class", "gt_match"], ai_rows)
    _write_csv(os.path.join(output_dir, "postproc_shape.csv"),
               JOIN + ["feat_roundness"], shape_rows)
    _write_csv(os.path.join(output_dir, "postproc_temporal.csv"),
               JOIN + ["dfeat_conf_delta_n3"], temporal_rows)
    _write_csv(os.path.join(output_dir, "decision.csv"),
               JOIN + ["dec_kept", "dec_reason", "dec_score", "gt_match"], decision_rows)

    manifest = {
        "run_id": req.run_id,
        "param_values": req.param_values,
        "n_items": len(_FIXTURE),
        "stages": [
            {"stage": "ai_inference", "csv": "ai_inference.csv", "grain": "entity", "depends_on": []},
            {"stage": "postproc_shape", "csv": "postproc_shape.csv", "grain": "entity", "depends_on": ["ai_inference"]},
            {"stage": "postproc_temporal", "csv": "postproc_temporal.csv", "grain": "entity", "depends_on": ["postproc_shape"]},
            {"stage": "decision", "csv": "decision.csv", "grain": "entity",
             "depends_on": ["ai_inference", "postproc_shape", "postproc_temporal"]},
        ],
    }
    manifest_path = os.path.join(output_dir, "manifest.json")
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, ensure_ascii=False, indent=2)

    return RunResult(
        run_id=req.run_id,
        status=RunStatus.evaluated,
        metrics=Metrics.from_counts(tp=tp, fp=fp, fn=fn, tn=tn),
        csv_manifest_uri=os.path.abspath(manifest_path),  # M2: points at the manifest
        n_items=len(_FIXTURE),
    )
