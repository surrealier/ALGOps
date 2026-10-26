"""Multi-stage CSV convention + deterministic cross-stage aggregation (plan §3.2, M2).

Each pipeline stage emits one CSV sharing the join columns ``run_id,item_id,frame_index,
entity_id,stage``. ``entity_id`` is the spine that joins AI output → features → decision for the
SAME detection. A ``manifest.json`` declares the stage DAG. Typed column prefixes:
``in_*``, ``ai_*``, ``feat_*``, ``dfeat_*``, ``dec_*``, ``gt_*``.

Aggregation is plain pandas (no LLM, no code-exec). When a ``decision`` stage with ``dec_kept``
and ``gt_match`` exists the confusion matrix is recomputed independently of what the program
reported; drop reasons are attributed to parameters through each param's declared
``drop_reasons`` (no project-specific mapping lives in ALGOps).
"""

from __future__ import annotations

import io
import json
import os
from typing import Any

import pandas as pd

from algops.core import Metrics

JOIN_COLS = ["run_id", "item_id", "frame_index", "entity_id", "stage"]
_FEATURE_PREFIXES = ("feat_", "dfeat_", "ai_")


def resolve_sibling_uri(manifest_uri: str, filename: str) -> str:
    """Given a manifest uri/path, return the uri/path of a sibling stage CSV.

    ``filename`` comes from a program-written manifest, so it must stay next to the manifest:
    a relative path without ``..`` segments (raises ``ValueError`` otherwise).
    """
    name = str(filename or "").replace("\\", "/")
    if not name or name.startswith("/") or ":" in name or any(p in ("", ".", "..") for p in name.split("/")):
        raise ValueError(f"invalid telemetry stage file name {filename!r}")
    filename = name
    if "://" in manifest_uri:
        return manifest_uri.rsplit("/", 1)[0] + "/" + filename
    return os.path.join(os.path.dirname(manifest_uri), filename)


def aggregate_multistage(
    manifest: dict,
    stage_texts: dict[str, str],
    reason_map: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Join stages on entity_id; compute confusion, drop reasons, param sensitivity, features."""
    frames = {stage: pd.read_csv(io.StringIO(text)) for stage, text in stage_texts.items()}
    decision = frames.get("decision")
    out: dict[str, Any] = {
        "n_items": int(manifest.get("n_items") or (len(decision) if decision is not None else 0)),
        "metrics": None,
        "drop_reasons": {},
        "sensitivity": {},
        "stage_counts": {stage: int(len(df)) for stage, df in frames.items()},
        "feature_means": {},
    }
    if decision is None or "dec_kept" not in decision:
        return out

    kept = decision["dec_kept"].astype(int) == 1
    if "gt_match" in decision:
        gt = decision["gt_match"].astype(int) == 1
        out["metrics"] = Metrics.from_counts(
            tp=int((kept & gt).sum()),
            fp=int((kept & ~gt).sum()),
            fn=int((~kept & gt).sum()),
            tn=int((~kept & ~gt).sum()),
        )

    if "dec_reason" in decision:
        drop = decision.loc[~kept]
        out["drop_reasons"] = {str(k): int(v) for k, v in drop["dec_reason"].value_counts().items()}
    # Parameter sensitivity: how many detections each parameter's filter dropped this run.
    sensitivity: dict[str, int] = {}
    for reason, count in out["drop_reasons"].items():
        param = (reason_map or {}).get(reason)
        if param:
            sensitivity[param] = sensitivity.get(param, 0) + count
    out["sensitivity"] = sensitivity

    # Feature distributions (kept vs dropped), joined across stages on entity_id.
    feature_means: dict[str, float] = {}
    base = decision[["entity_id"]].assign(_kept=kept.astype(int).values)
    for stage, df in frames.items():
        if stage == "decision" or "entity_id" not in df:
            continue
        cols = [c for c in df.columns if c.startswith(_FEATURE_PREFIXES)]
        numeric = [c for c in cols if pd.api.types.is_numeric_dtype(df[c])]
        if not numeric:
            continue
        merged = base.merge(df[["entity_id", *numeric]], on="entity_id", how="left")
        for col in numeric[:12]:
            for label, sub in merged.groupby("_kept"):
                mean = sub[col].mean()
                if pd.notna(mean):
                    feature_means[f"{col}|kept={int(label)}"] = float(mean)
    out["feature_means"] = feature_means
    return out


def load_manifest(text: str) -> dict | None:
    try:
        obj = json.loads(text)
        return obj if isinstance(obj, dict) and "stages" in obj else None
    except Exception:
        return None
