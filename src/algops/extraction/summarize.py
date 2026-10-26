"""Deterministic CSV aggregation + run-report authoring (RCE-free path, plan §3.2).

LangChain's pandas DataFrame agent is sunset and carries an arbitrary-code-execution risk, so
ALGOps aggregates with plain pandas and renders Markdown deterministically. LLM planners may
author richer prose on top — but the numbers are always computed here, never by the model.
"""

from __future__ import annotations

import io
from typing import Any

import pandas as pd

from algops.core import GoalDirection, Metrics, MetricName, Objective, normalize_metrics

_PREFERRED_ORDER = ["precision", "recall", "f1", "accuracy", "fp_rate", "fn_rate"]
_COUNT_KEYS = ("tp", "fp", "fn", "tn")


def aggregate_decision_csv(csv_text: str) -> dict[str, Any]:
    """Recompute the confusion matrix + decision-reason breakdown from a single decision CSV."""
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


def _fmt(v: float) -> str:
    if float(v).is_integer() and abs(v) >= 1:
        return f"{int(v)}"
    return f"{v:.4f}"


def metrics_table(metrics: dict[str, float]) -> list[str]:
    lines = ["| 지표 | 값 |", "| --- | --- |"]
    shown: set[str] = set()
    for name in _PREFERRED_ORDER:
        if name in metrics:
            lines.append(f"| {name} | {_fmt(metrics[name])} |")
            shown.add(name)
    if all(k in metrics for k in _COUNT_KEYS):
        lines.append("| tp / fp / fn / tn | " + " / ".join(_fmt(metrics[k]) for k in _COUNT_KEYS) + " |")
        shown.update(_COUNT_KEYS)
    for name in sorted(metrics):
        if name not in shown:
            lines.append(f"| {name} | {_fmt(metrics[name])} |")
    return lines


def summarize_markdown(
    *,
    project_name: str,
    iteration_index: int,
    param_values: dict[str, Any],
    metrics: Any,
    target_metric: MetricName | str,
    target_value: float | None,
    direction: GoalDirection | str,
    drop_reasons: dict[str, int] | None = None,
    n_items: int | None = None,
    objective: Objective | None = None,
    algorithm_id: str | None = None,
    suite_id: str | None = None,
    commit_sha: str | None = None,
) -> str:
    """Render a deterministic Korean run-report (one document → chunked to Qdrant)."""
    m = normalize_metrics(metrics)
    tm = target_metric.value if isinstance(target_metric, MetricName) else str(target_metric)
    obj = objective or Objective(metric=tm, direction=GoalDirection(direction), target=target_value)
    value = m.get(obj.metric)
    arrow = "≤" if obj.direction is GoalDirection.minimize else "≥"
    if obj.target is None:
        hit = "목표값 없음"
    else:
        hit = "달성" if obj.reached(m) else "미달"
    target_txt = f"목표 {arrow} {obj.target:g}" if obj.target is not None else obj.direction.value
    value_txt = f"{value:.4f}" if value is not None else "(보고되지 않음)"
    params_tbl = "\n".join(f"| `{k}` | {v} |" for k, v in sorted(param_values.items()))
    where = " · ".join(x for x in (
        f"알고리즘 `{algorithm_id}`" if algorithm_id else "",
        f"스위트 `{suite_id}`" if suite_id else "",
        f"커밋 `{commit_sha[:10]}`" if commit_sha else "",
    ) if x)
    lines = [f"# 실험 리포트 — {project_name} (iteration {iteration_index})", ""]
    if where:
        lines += [f"- {where}"]
    lines += [
        f"- **목표 지표:** `{obj.metric}` ({target_txt}) → 현재 **{value_txt}** ({hit})",
    ]
    if obj.constraints:
        status = ", ".join(
            f"{c.describe()} {'✓' if c.satisfied(m) else '✗'}" for c in obj.constraints
        )
        lines.append(f"- **제약:** {status}")
    lines += [
        f"- **평가 항목 수:** {n_items if n_items is not None else '-'}",
        "",
        "## 파라미터",
        "| 키 | 값 |",
        "| --- | --- |",
        params_tbl or "| (없음) | |",
        "",
        "## 지표",
        *metrics_table(m),
    ]
    if drop_reasons:
        lines += ["", "## 제거 사유 분포"]
        lines += [f"- {reason}: {count}" for reason, count in drop_reasons.items()]
    return "\n".join(lines) + "\n"
