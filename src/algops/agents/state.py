"""The shared LangGraph state for the experiment loop (plan §4).

Immutable config is set once at invoke; working fields are overwritten each pass; ``history``
is the only accumulator (append via ``operator.add``). Best-so-far is tracked explicitly so
``finalize`` can return the winning run regardless of where in the search it occurred.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict


class AlgOpsState(TypedDict, total=False):
    # --- immutable config (set at invoke) ---
    experiment_id: str
    project_id: str
    project_name: str
    goal_text: str
    target_metric: str
    target_value: float
    goal_direction: str
    max_iteration: int
    test_set_uri: str
    primary_param_key: str
    param_spec: dict  # ParamSpec.model_dump()
    require_approval: bool  # HITL gate before the Developer applies a change (M5)

    # --- loop control ---
    iteration: int
    pm_action: str
    stop_reason: str | None
    converged: bool
    search_dir: int
    search_step: float

    # --- per-iteration working fields ---
    current_params: dict
    proposed_params: dict
    last_run_id: str
    last_metrics: dict
    last_score: float
    csv_drop_reasons: dict
    csv_n_items: int
    csv_sensitivity: dict
    csv_summary_md: str
    rag_context: str
    rag_anchor_ids: list
    analyst_findings: str

    # --- accumulators / best-so-far ---
    history: Annotated[list, operator.add]
    best_run_id: str | None
    best_score: float | None
    best_params: dict
    best_metrics: dict

    # --- output ---
    final_params: dict
    final_metrics: dict
    winning_run_id: str | None
    report_md: str


def initial_state(
    *,
    experiment_id: str,
    project_id: str,
    project_name: str,
    goal_text: str,
    target_metric: str,
    target_value: float,
    goal_direction: str,
    max_iteration: int,
    test_set_uri: str,
    primary_param_key: str,
    param_spec: dict,
    require_approval: bool = False,
) -> AlgOpsState:
    return AlgOpsState(
        experiment_id=experiment_id,
        project_id=project_id,
        project_name=project_name,
        goal_text=goal_text,
        target_metric=target_metric,
        target_value=target_value,
        goal_direction=goal_direction,
        max_iteration=max_iteration,
        test_set_uri=test_set_uri,
        primary_param_key=primary_param_key,
        param_spec=param_spec,
        require_approval=require_approval,
        iteration=0,
        history=[],
        best_score=None,
        best_run_id=None,
    )
