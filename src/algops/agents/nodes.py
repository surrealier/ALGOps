"""The loop nodes (plan §4). M0 agents are deterministic stubs; M1 swaps PM/Analyst/
pandas_summarize for LLM agents behind the same node boundaries.

    rag_retrieve → pm_propose → developer → run_eval → extract_csv
                 → pandas_summarize → analyst → pm_decide ─(continue|finalize)

``pm_propose`` runs a single-parameter directional hill-climb; ``pm_decide`` updates the
best-so-far and proposes continue/stop; the *router* (graph.py) is what actually enforces
max_iteration and target-reached, so the cap can never be talked past by an agent.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from algops.core import GoalDirection, MetricName, ParamSpec, RunRequest, RunStatus, new_id
from algops.core.schemas import Metrics
from algops.extraction import (
    aggregate_decision_csv,
    aggregate_multistage,
    is_better,
    load_manifest,
    objective_value,
    resolve_sibling_uri,
    summarize_markdown,
    target_reached,
)

from .executor import RunExecutor
from .hooks import LoopHooks, NullHooks
from .state import AlgOpsState


@dataclass
class LoopDeps:
    executor: RunExecutor
    hooks: LoopHooks
    # Optional LLM planner (algops.llm.LLMPlanner). None => deterministic stubs (M0 default).
    planner: Any = None


# Heuristic first move: which way to nudge the primary threshold to improve the goal metric.
_INCREASE_HELPS = {MetricName.fp_rate, MetricName.fp, MetricName.precision}
_DECREASE_HELPS = {MetricName.fn_rate, MetricName.fn, MetricName.recall}


def _initial_dir(target_metric: str) -> int:
    tm = MetricName(target_metric)
    if tm in _INCREASE_HELPS:
        return 1
    if tm in _DECREASE_HELPS:
        return -1
    return 1  # f1 / tp / tn: default to increasing first


def make_nodes(deps: LoopDeps) -> dict[str, Any]:
    hooks = deps.hooks

    async def rag_retrieve(state: AlgOpsState) -> dict:
        # M3: hybrid retrieve over prior run reports + learned parameter influence (graph).
        try:
            ctx = hooks.retrieve_rag(
                project_id=state["project_id"],
                goal_text=state.get("goal_text", ""),
                target_metric=state["target_metric"],
                top_k=5,
            )
        except Exception:
            ctx = None
        if not ctx:
            return {"rag_context": "", "rag_anchor_ids": []}
        return {"rag_context": ctx.get("text", ""), "rag_anchor_ids": ctx.get("anchor_ids", [])}

    async def pm_propose(state: AlgOpsState) -> dict:
        spec = ParamSpec(**state["param_spec"])
        key = state["primary_param_key"]
        pdef = spec.by_key()[key]
        direction = state["goal_direction"]
        it = state.get("iteration", 0) + 1
        history = state.get("history", [])

        if not history:
            proposed = dict(state.get("current_params") or spec.defaults())
            proposed[key] = pdef.default
            return {
                "iteration": it,
                "proposed_params": proposed,
                "search_dir": _initial_dir(state["target_metric"]),
                "search_step": float(pdef.step or 0.0),
                "converged": False,
            }

        # LLM path (optional): the PM proposes the next value, grounded in history + RAG.
        if deps.planner is not None:
            try:
                proposal = await deps.planner.propose(state, spec)
            except Exception:
                proposal = None
            if proposal is not None and proposal.param_key in spec.by_key():
                pd2 = spec.by_key()[proposal.param_key]
                proposed = dict(history[-1]["params"])
                proposed[proposal.param_key] = pd2.clamp(proposal.new_value)
                return {
                    "iteration": it,
                    "proposed_params": proposed,
                    "converged": False,
                    "search_dir": state.get("search_dir", _initial_dir(state["target_metric"])),
                    "search_step": state.get("search_step", float(pdef.step or 0.0)),
                }

        last = history[-1]
        last_val = float(last["params"][key])
        search_dir = state.get("search_dir", _initial_dir(state["target_metric"]))
        step = state.get("search_step", float(pdef.step or 0.0))
        floor = float(pdef.step or 0.0) * 0.125

        if len(history) >= 2:
            improved = is_better(history[-1]["objective"], history[-2]["objective"], direction)
            if not improved:
                search_dir = -search_dir
                step = max(step * 0.5, floor)

        next_val = pdef.clamp(last_val + search_dir * step)
        converged = next_val == last_val  # hit a boundary / step underflow → nowhere to go
        proposed = dict(last["params"])
        proposed[key] = next_val
        return {
            "iteration": it,
            "proposed_params": proposed,
            "search_dir": search_dir,
            "search_step": step,
            "converged": converged,
        }

    async def developer(state: AlgOpsState) -> dict:
        # Optional HITL gate (M5): pause for human approval BEFORE applying the change. The
        # interrupt re-runs this node on resume, so the only side effect (current_params) is
        # idempotent. M4 patches the target repo in an isolated git worktree.
        if state.get("require_approval"):
            from langgraph.types import interrupt

            interrupt(
                {
                    "type": "approval_required",
                    "experiment_id": state["experiment_id"],
                    "iteration": state["iteration"],
                    "proposed_params": state["proposed_params"],
                }
            )
        return {"current_params": dict(state["proposed_params"])}

    async def run_eval(state: AlgOpsState) -> dict:
        run_id = new_id()
        params = state["current_params"]
        it = state["iteration"]
        hooks.on_run_created(
            run_id=run_id,
            iteration=it,
            params=params,
            derived_from=state.get("last_run_id"),
        )
        req = RunRequest(
            run_id=run_id,
            experiment_id=state["experiment_id"],
            project_id=state["project_id"],
            param_values=params,
            test_set_uri=state["test_set_uri"],
        )
        result = await deps.executor.run(req)
        metrics = result.metrics or Metrics()
        obj = objective_value(metrics, state["target_metric"])
        hooks.on_run_result(
            run_id=run_id,
            status=result.status.value if isinstance(result.status, RunStatus) else str(result.status),
            metrics=metrics,
            score=obj,
            n_items=result.n_items,
            csv_manifest_uri=result.csv_manifest_uri,
        )
        await hooks.emit(
            {
                "type": "run_evaluated",
                "experiment_id": state["experiment_id"],
                "iteration": it,
                "run_id": run_id,
                "params": params,
                "metrics": metrics.model_dump(),
                "objective": obj,
            }
        )
        entry = {
            "iteration": it,
            "run_id": run_id,
            "params": dict(params),
            "objective": obj,
            "metrics": metrics.model_dump(),
            "csv_uri": result.csv_manifest_uri,
        }
        return {
            "last_run_id": run_id,
            "last_metrics": metrics.model_dump(),
            "last_score": obj,
            "history": [entry],
        }

    async def extract_csv(state: AlgOpsState) -> dict:
        uri = state["history"][-1].get("csv_uri") if state.get("history") else None
        text = hooks.fetch_text(uri)
        if not text:
            return {}
        manifest = load_manifest(text)
        if manifest is None:
            # Back-compat: a single decision CSV instead of a manifest.
            try:
                agg = aggregate_decision_csv(text)
            except Exception:
                return {}
            return {"csv_drop_reasons": agg.get("drop_reasons", {}), "csv_n_items": agg.get("n_items", 0)}

        stage_texts: dict[str, str] = {}
        for stage in manifest.get("stages", []):
            stage_text = hooks.fetch_text(resolve_sibling_uri(uri, stage["csv"]))
            if stage_text:
                stage_texts[stage["stage"]] = stage_text
        try:
            agg = aggregate_multistage(manifest, stage_texts)
        except Exception:
            return {}
        return {
            "csv_drop_reasons": agg.get("drop_reasons", {}),
            "csv_n_items": agg.get("n_items", 0),
            "csv_sensitivity": agg.get("sensitivity", {}),
        }

    async def pandas_summarize(state: AlgOpsState) -> dict:
        metrics = Metrics(**state["last_metrics"])
        md: str | None = None
        if deps.planner is not None:
            try:
                md = await deps.planner.summarize(state)
            except Exception:
                md = None
        if not md:
            md = summarize_markdown(
                project_name=state.get("project_name", "project"),
                iteration_index=state["iteration"],
                param_values=state["current_params"],
                metrics=metrics,
                target_metric=state["target_metric"],
                target_value=state["target_value"],
                direction=state["goal_direction"],
                drop_reasons=state.get("csv_drop_reasons"),
                n_items=state.get("csv_n_items"),
            )
        hooks.put_summary(state["last_run_id"], md)

        # Index this run as a RAG document + graph node (one report = one document, plan §3.3).
        history = state.get("history", [])
        prev = history[-2] if len(history) >= 2 else None
        changed: list[str] = []
        deltas: dict[str, float] = {}
        if prev is not None:
            changed = [k for k, v in state["current_params"].items() if prev["params"].get(k) != v]
            prev_m = Metrics(**prev["metrics"])
            for name in {state["target_metric"], "fp_rate", "fn_rate", "f1"}:
                deltas[name] = metrics.get(name) - prev_m.get(name)
        try:
            hooks.index_run_report(
                run_id=state["last_run_id"],
                experiment_id=state["experiment_id"],
                project_id=state["project_id"],
                iteration_index=state["iteration"],
                goal_tag=state.get("goal_text", ""),
                params=dict(state["current_params"]),
                params_changed=changed,
                metric_deltas=deltas,
                primary_metric=state["target_metric"],
                primary_metric_value=metrics.get(state["target_metric"]),
                report_md=md,
                derived_from=(prev["run_id"] if prev else None),
            )
        except Exception:
            pass
        return {"csv_summary_md": md}

    async def analyst(state: AlgOpsState) -> dict:
        # M1: LLM causal analysis grounded in rag_context; deterministic one-liner otherwise.
        if deps.planner is not None:
            try:
                findings = await deps.planner.analyze(state)
            except Exception:
                findings = None
            if findings:
                return {"analyst_findings": findings}
        m = Metrics(**state["last_metrics"])
        tm = state["target_metric"]
        note = (
            f"iteration {state['iteration']}: {tm}={m.get(tm):.4f}, "
            f"precision={m.precision:.4f}, recall={m.recall:.4f}, f1={m.f1:.4f}"
        )
        return {"analyst_findings": note}

    async def pm_decide(state: AlgOpsState) -> dict:
        m = Metrics(**state["last_metrics"])
        score = state["last_score"]
        direction = state["goal_direction"]
        updates: dict[str, Any] = {}

        if is_better(score, state.get("best_score"), direction):
            updates.update(
                best_score=score,
                best_run_id=state["last_run_id"],
                best_params=dict(state["current_params"]),
                best_metrics=state["last_metrics"],
            )

        reached = target_reached(m, state["target_metric"], state["target_value"], direction)
        action: str | None = None
        reason: str | None = None
        if reached:
            action, reason = "stop", "target_reached"
        elif deps.planner is not None:
            # LLM decides continue/stop; caps + target stay enforced by the router.
            try:
                decision = await deps.planner.decide(state)
            except Exception:
                decision = None
            if decision is not None:
                action = decision.action
                reason = (decision.stop_reason or "pm_stop") if decision.action == "stop" else None
        if action is None:
            if state.get("converged"):
                action, reason = "stop", "converged"
            else:
                action, reason = "continue", None

        rationale = (
            f"{state['target_metric']}={m.get(state['target_metric']):.4f} "
            f"(target {state['target_value']}, {direction})"
        )
        hooks.on_iteration(
            iteration=state["iteration"],
            analyst_findings=state.get("analyst_findings"),
            pm_action=action,
            stop_reason=reason,
            rationale=rationale,
            proposed_run_id=state["last_run_id"],
        )
        await hooks.emit(
            {
                "type": "iteration_decided",
                "experiment_id": state["experiment_id"],
                "iteration": state["iteration"],
                "pm_action": action,
                "stop_reason": reason,
                "best_score": updates.get("best_score", state.get("best_score")),
            }
        )
        updates.update(pm_action=action, stop_reason=reason)
        return updates

    async def finalize(state: AlgOpsState) -> dict:
        params = state.get("best_params") or state.get("current_params") or {}
        metrics_dump = state.get("best_metrics") or state.get("last_metrics") or {}
        metrics = Metrics(**metrics_dump) if metrics_dump else Metrics()
        winning = state.get("best_run_id") or state.get("last_run_id")

        reason = state.get("stop_reason")
        if reason is None:
            reason = (
                "max_iteration"
                if state.get("iteration", 0) >= state["max_iteration"]
                else "pm_stop"
            )

        report = summarize_markdown(
            project_name=state.get("project_name", "project"),
            iteration_index=state.get("iteration", 0),
            param_values=params,
            metrics=metrics,
            target_metric=state["target_metric"],
            target_value=state["target_value"],
            direction=state["goal_direction"],
        )
        report = f"# 최종 결과 (종료 사유: {reason})\n\n" + report

        refs = hooks.on_finalize(
            winning_run_id=winning,
            params=params,
            metrics=metrics,
            stop_reason=reason,
            report_md=report,
        )
        await hooks.emit(
            {
                "type": "finalized",
                "experiment_id": state["experiment_id"],
                "winning_run_id": winning,
                "stop_reason": reason,
                "params": params,
                "metrics": metrics.model_dump(),
                "notify_refs": refs,
            }
        )
        return {
            "final_params": params,
            "final_metrics": metrics.model_dump(),
            "winning_run_id": winning,
            "report_md": report,
            "stop_reason": reason,
        }

    return {
        "rag_retrieve": rag_retrieve,
        "pm_propose": pm_propose,
        "developer": developer,
        "run_eval": run_eval,
        "extract_csv": extract_csv,
        "pandas_summarize": pandas_summarize,
        "analyst": analyst,
        "pm_decide": pm_decide,
        "finalize": finalize,
    }


def route_after_pm(state: AlgOpsState) -> str:
    """Stop conditions enforced in the router (defense in depth), not by the agent."""
    if state.get("pm_action") == "stop":
        return "finalize"
    if state.get("iteration", 0) >= state["max_iteration"]:
        return "finalize"
    last = state.get("last_metrics")
    if last and target_reached(
        Metrics(**last),
        state["target_metric"],
        state["target_value"],
        state["goal_direction"],
    ):
        return "finalize"
    return "continue"


def make_default_deps() -> LoopDeps:
    """Deps with a null executor placeholder — callers must supply a real executor."""
    raise RuntimeError("supply LoopDeps(executor=..., hooks=...) explicitly")
