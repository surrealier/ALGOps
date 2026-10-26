"""The shared LangGraph state for the experiment loop (plan §4, contract v2).

Immutable config is set once at invoke; working fields are overwritten each pass; ``history``
is the only accumulator (append via ``operator.add``) — one entry per evaluated candidate. The
best-so-far is tracked explicitly (ranked by the :class:`~algops.core.Objective`, feasibility
first) so ``finalize`` returns the winner regardless of where in the search it occurred.
"""

from __future__ import annotations

import operator
from typing import Annotated, Any, TypedDict

from algops.core import GoalDirection, Objective


class AlgOpsState(TypedDict, total=False):
    # --- immutable config (set at invoke) ---
    experiment_id: str
    project_id: str
    project_name: str
    goal_text: str
    target_metric: str
    target_value: float | None
    goal_direction: str
    objective: dict  # Objective.model_dump(mode="json")
    max_iteration: int
    test_set_uri: str
    primary_param_key: str | None
    param_spec: dict  # ParamSpec.model_dump(mode="json")
    require_approval: bool  # HITL gate before the Developer applies a change (M5)
    algorithm_id: str
    suite_id: str
    validation_suite_ids: list
    strategy: str
    search_keys: list
    batch_size: int
    seed: int
    commit_sha: str | None
    attempt: int
    reason_map: dict  # dec_reason -> param key

    # --- loop control ---
    iteration: int
    pm_action: str
    stop_reason: str | None
    converged: bool
    search_state: dict
    failed_iterations: int

    # --- per-iteration working fields ---
    current_params: dict
    proposed_params: dict
    proposed_batch: list
    current_batch: list
    last_run_id: str
    last_metrics: dict
    last_score: float | None
    csv_drop_reasons: dict
    csv_n_items: int
    csv_sensitivity: dict
    csv_summary_md: str
    rag_context: str
    rag_anchor_ids: list
    analyst_findings: str
    proposal_rationale: str

    # --- accumulators / best-so-far ---
    history: Annotated[list, operator.add]
    best_run_id: str | None
    best_score: float | None
    best_params: dict
    best_metrics: dict

    # --- output ---
    validation: dict
    final_params: dict
    final_metrics: dict
    winning_run_id: str | None
    report_md: str


def state_objective(state: AlgOpsState | dict) -> Objective:
    if state.get("objective"):
        return Objective.model_validate(state["objective"])
    return Objective(
        metric=state["target_metric"],
        direction=GoalDirection(state["goal_direction"]),
        target=state.get("target_value"),
    )


def initial_state(
    *,
    experiment_id: str,
    project_id: str,
    project_name: str,
    goal_text: str,
    target_metric: str,
    target_value: float | None,
    goal_direction: str,
    max_iteration: int,
    test_set_uri: str = "",
    primary_param_key: str | None = None,
    param_spec: dict,
    require_approval: bool = False,
    objective: Objective | dict | None = None,
    algorithm_id: str = "default",
    suite_id: str = "default",
    validation_suite_ids: list[str] | None = None,
    strategy: str = "coordinate",
    search_keys: list[str] | None = None,
    batch_size: int = 1,
    seed: int = 0,
    commit_sha: str | None = None,
    attempt: int = 0,
    reason_map: dict[str, str] | None = None,
) -> AlgOpsState:
    if objective is None:
        obj = Objective(metric=target_metric, direction=GoalDirection(goal_direction), target=target_value)
    elif isinstance(objective, Objective):
        obj = objective
    else:
        obj = Objective.model_validate(objective)
    keys = list(search_keys) if search_keys else ([primary_param_key] if primary_param_key else [])
    if not keys:
        keys = [p["key"] for p in (param_spec.get("params") or [])]
    return AlgOpsState(
        experiment_id=experiment_id,
        project_id=project_id,
        project_name=project_name,
        goal_text=goal_text,
        target_metric=obj.metric,
        target_value=obj.target,
        goal_direction=obj.direction.value,
        objective=obj.model_dump(mode="json"),
        max_iteration=max_iteration,
        test_set_uri=test_set_uri,
        primary_param_key=primary_param_key or (keys[0] if keys else None),
        param_spec=param_spec,
        require_approval=require_approval,
        algorithm_id=algorithm_id,
        suite_id=suite_id,
        validation_suite_ids=list(validation_suite_ids or []),
        strategy=strategy,
        search_keys=keys,
        batch_size=max(1, int(batch_size)),
        seed=seed,
        commit_sha=commit_sha,
        attempt=attempt,
        reason_map=dict(reason_map or {}),
        iteration=0,
        history=[],
        search_state={},
        failed_iterations=0,
        best_score=None,
        best_run_id=None,
        validation={},
    )


def ensure_dict(value: Any) -> dict:
    return dict(value) if isinstance(value, dict) else {}
