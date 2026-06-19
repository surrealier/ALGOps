"""Multi-stage CSV convention + deterministic cross-stage aggregation (plan §3.2, M2).

Each pipeline stage emits one CSV sharing the join columns ``run_id,item_id,frame_index,
entity_id,stage``. ``entity_id`` is the spine that joins AI output → shape features → temporal
features → decision for the SAME detection. A ``manifest.json`` declares the stage DAG.
Typed column prefixes: in_*, ai_*, feat_*, dfeat_*, dec_*, gt_*.

Aggregation is plain pandas (no LLM, no code-exec): join on entity_id, recompute the confusion
matrix from the decision stage, break down drop reasons, and attribute each drop to the
parameter/stage responsible (parameter sensitivity).
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

import pandas as pd

from algops.core import Metrics

JOIN_COLS = ["run_id", "item_id", "frame_index", "entity_id", "stage"]

# Decision drop reason -> the parameter that caused it (toy mapping; real projects declare theirs).
_REASON_TO_PARAM = {
    "below_threshold": "detector.confidence_threshold",
    "low_roundness": "postproc.roundness.min",
}


def resolve_sibling_uri(manifest_uri: str, filename: str) -> str:
    """Given a manifest uri/path, return the uri/path of a sibling stage CSV."""
    if "://" in manifest_uri:
        return manifest_uri.rsplit("/", 1)[0] + "/" + filename
    return os.path.join(os.path.dirname(manifest_uri), filename)


def aggregate_multistage(manifest: dict, stage_texts: dict[str, str]) -> dict[str, Any]:
    """Join stages on entity_id and compute confusion, drop reasons, and param sensitivity."""
    frames = {stage: pd.read_csv(io.StringIO(text)) for stage, text in stage_texts.items()}
    decision = frames.get("decision")
    if decision is None:
        raise ValueError("manifest has no 'decision' stage CSV")

    kept = decision["dec_kept"].astype(int) == 1
    gt = decision["gt_match"].astype(int) == 1
    metrics = Metrics.from_counts(
        tp=int((kept & gt).sum()),
        fp=int((kept & ~gt).sum()),
        fn=int((~kept & gt).sum()),
        tn=int((~kept & ~gt).sum()),
    )

    drop = decision.loc[~kept]
    drop_reasons = (
        {str(k): int(v) for k, v in drop["dec_reason"].value_counts().items()}
        if "dec_reason" in decision
        else {}
    )
    # Parameter sensitivity: how many detections each parameter's filter dropped this run.
    sensitivity: dict[str, int] = {}
    for reason, count in drop_reasons.items():
        param = _REASON_TO_PARAM.get(reason)
        if param:
            sensitivity[param] = sensitivity.get(param, 0) + count

    # A feature-distribution sanity figure joined across stages on entity_id.
    feature_means: dict[str, float] = {}
    shape = frames.get("postproc_shape")
    if shape is not None and "feat_roundness" in shape:
        merged = decision[["entity_id", "dec_kept"]].merge(
            shape[["entity_id", "feat_roundness"]], on="entity_id", how="left"
        )
        for label, sub in merged.groupby(merged["dec_kept"].astype(int)):
            feature_means[f"roundness_kept={int(label)}"] = float(sub["feat_roundness"].mean())

    return {
        "n_items": int(manifest.get("n_items", len(decision))),
        "metrics": metrics,
        "drop_reasons": drop_reasons,
        "sensitivity": sensitivity,
        "stage_counts": {stage: int(len(df)) for stage, df in frames.items()},
        "feature_means": feature_means,
    }


def load_manifest(text: str) -> dict | None:
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) and "stages" in obj else None
    except Exception:
        return None
