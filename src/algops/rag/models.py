"""RAG data shapes (plan §3.3). One run report = one document; the graph captures params,
their FP/FN influence, and run lineage."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RunReport(BaseModel):
    """A single run's summary report + structured facts (chunked to Qdrant, MERGE'd to Neo4j)."""

    run_id: str
    experiment_id: str
    project_id: str
    algorithm_id: str = "default"
    iteration_index: int = 0
    goal_tag: str = ""  # free-text goal of the campaign, e.g. "오탐 완화"
    params: dict[str, Any] = Field(default_factory=dict)
    params_changed: list[str] = Field(default_factory=list)  # vs the previous run
    metric_deltas: dict[str, float] = Field(default_factory=dict)  # metric -> Δ vs previous run
    primary_metric: str = ""
    primary_metric_value: float | None = None
    report_md: str = ""
    derived_from: str | None = None  # previous run_id (lineage)
    timestamp_index: int = 0  # monotonic ingestion order (no wall-clock needed)


class RagQuery(BaseModel):
    project_id: str
    algorithm_id: str | None = None  # None => every algorithm of the project
    goal_text: str = ""
    target_metric: str = ""
    top_k: int = 5


class ParamHint(BaseModel):
    """A learned hint: changing this parameter tended to help (or hurt) the target metric."""

    param_key: str
    direction: str  # "up" | "down"
    helped: int = 0
    hurt: int = 0


class RagContext(BaseModel):
    text: str = ""  # human/LLM-readable summary injected into Analyst/PM prompts
    anchor_run_ids: list[str] = Field(default_factory=list)
    param_hints: list[ParamHint] = Field(default_factory=list)
