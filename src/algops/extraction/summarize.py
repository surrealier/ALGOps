"""Deterministic CSV aggregation + run-report authoring (RCE-free path, plan §3.2).

LangChain's pandas DataFrame agent is sunset and carries an arbitrary-code-execution risk,
so M0 (and the M2 full convention) aggregate with plain pandas and render a Markdown report
deterministically. From M1 the compact aggregate tables are handed to the LLM via structured
output to author richer prose — but the numbers are always computed here, never by the model.

M0 CSV convention (single 'decision' stage), columns:
    run_id, item_id, frame_index, entity_id, stage,
    ai_confidence, dec_kept, dec_reason, gt_match
where dec_kept=1 is predicted-positive and gt_match=1 is a true target.
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd

from algops.core import GoalDirection, Metrics, MetricName


def aggregate_decision_csv(csv_text: str) -> dict[str, Any]:
    """Recompute the confusion matrix + decision-reason breakdown from the raw CSV.

    This is an independent check of the slave-reported metrics (the slave and the master
    derive the same numbers two different ways — a mismatch surfaces a telemetry bug).
    """
    df = pd.read_csv(io.StringIO(csv_text))
    kept = df["dec_kept"].astype(int) == 1
    gt = df["gt_match"].astype(int) == 1
    tp = int((kept & gt).sum())
    fp = int((kept & ~gt).sum())
    fn = int((~kept & gt).sum())
    tn = int((~kept & ~gt).sum())
    reasons = (
        df.loc[~kept, "dec_reason"].value_counts().to_dict() if "dec_reason" in df else {}
    )
    return {
        "n_items": int(len(df)),
        "metrics": Metrics.from_counts(tp, fp, fn, tn),
        "drop_reasons": {str(k): int(v) for k, v in reasons.items()},
        "confidence_mean": float(df["ai_confidence"].mean()) if "ai_confidence" in df else None,
    }


def summarize_markdown(
    *,
    project_name: str,
    iteration_index: int,
    param_values: dict[str, Any],
    metrics: Metrics,
    target_metric: MetricName | str,
    target_value: float,
    direction: GoalDirection | str,
    drop_reasons: dict[str, int] | None = None,
    n_items: int | None = None,
) -> str:
    """Render a deterministic Korean run-report (one document → chunked to Qdrant in M3)."""
    tm = MetricName(target_metric).value
    value = metrics.get(target_metric)
    arrow = "≤" if GoalDirection(direction) is GoalDirection.minimize else "≥"
    hit = "달성" if (
        (value <= target_value)
        if GoalDirection(direction) is GoalDirection.minimize
        else (value >= target_value)
    ) else "미달"
    params_tbl = "\n".join(f"| `{k}` | {v} |" for k, v in sorted(param_values.items()))
    lines = [
        f"# 실험 리포트 — {project_name} (iteration {iteration_index})",
        "",
        f"- **목표 지표:** `{tm}` (목표 {arrow} {target_value}) → 현재 **{value:.4f}** ({hit})",
        f"- **평가 항목 수:** {n_items if n_items is not None else '-'}",
        "",
        "## 파라미터",
        "| 키 | 값 |",
        "| --- | --- |",
        params_tbl or "| (없음) | |",
        "",
        "## 지표",
        "| 지표 | 값 |",
        "| --- | --- |",
        f"| precision | {metrics.precision:.4f} |",
        f"| recall | {metrics.recall:.4f} |",
        f"| f1 | {metrics.f1:.4f} |",
        f"| fp_rate (오탐율) | {metrics.fp_rate:.4f} |",
        f"| fn_rate (미탐율) | {metrics.fn_rate:.4f} |",
        f"| tp / fp / fn / tn | {metrics.tp} / {metrics.fp} / {metrics.fn} / {metrics.tn} |",
    ]
    if drop_reasons:
        lines += ["", "## 제거 사유 분포"]
        lines += [f"- {reason}: {count}" for reason, count in drop_reasons.items()]
    return "\n".join(lines) + "\n"
