"""The loop nodes (plan §4, contract v2).

    rag_retrieve → pm_propose → developer → run_eval → extract_csv → pandas_summarize
                 → analyst → pm_decide ─(continue | finalize → validate → finalize)

* ``pm_propose`` — iteration 1 always evaluates the code's current defaults (the baseline);
  afterwards the optional LLM planner proposes one multi-parameter change and the deterministic
  strategy (coordinate / random / grid) fills the rest of the batch.
* ``run_eval``   — evaluates the whole batch concurrently (runners execute in parallel).
* ``pm_decide``  — ranks by the :class:`Objective` (feasible first), proposes continue/stop.
  The *router* enforces max_iteration / target-reached, so an agent can never talk past them.
* ``validate``   — re-evaluates the winner (and the baseline) on held-out suites.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Any

from algops.core import ParamSpec, RunRequest, RunResult, RunStatus, new_id
from algops.extraction import (
    aggregate_decision_csv,
    aggregate_multistage,
    load_manifest,
    resolve_sibling_uri,
    summarize_markdown,
)
from algops.search import best_entry, params_key
from algops.search import propose as strategy_propose

from .executor import RunExecutor
from .hooks import LoopHooks
from .state import AlgOpsState, state_objective

MAX_FAILED_ITERATIONS = 3


@dataclass
class LoopDeps:
    executor: RunExecutor
    hooks: LoopHooks
    # Optional LLM planner (algops.llm.LLMPlanner). None => deterministic strategies only.
    planner: Any = None


def _spec(state: AlgOpsState) -> ParamSpec:
    return ParamSpec(**state["param_spec"])


def _iteration_entries(state: AlgOpsState) -> list[dict]:
    it = state.get("iteration", 0)
    return [h for h in state.get("history", []) if h.get("iteration") == it and h.get("kind", "search") == "search"]


def make_nodes(deps: LoopDeps) -> dict[str, Any]:
    hooks = deps.hooks

    async def _evaluate(state: AlgOpsState, params: dict, *, suite_id: str, kind: str,
                        derived_from: str | None, iteration: int) -> dict:
        obj = state_objective(state)
        run_id = new_id()
        hooks.on_run_created(run_id=run_id, iteration=iteration, params=params,
                             derived_from=derived_from, kind=kind, suite_id=suite_id)
        req = RunRequest(
            run_id=run_id,
            experiment_id=state["experiment_id"],
            project_id=state["project_id"],
            param_values=params,
            test_set_uri=state.get("test_set_uri", ""),
            algorithm_id=state.get("algorithm_id", "default"),
            suite_id=suite_id,
            kind=kind,  # type: ignore[arg-type]
            seed=int(state.get("seed") or 0),
        )
        try:
            result = await deps.executor.run(req)
        except Exception as exc:  # noqa: BLE001 — an executor failure is a failed run, not a crash
            result = RunResult(run_id=run_id, status=RunStatus.failed, error=f"{type(exc).__name__}: {exc}",
                               error_kind="infra")
        metrics = dict(result.metrics or {})
        ok = result.status == RunStatus.evaluated and bool(metrics)
        value = metrics.get(obj.metric)
        hooks.on_run_result(
            run_id=run_id,
            status=result.status.value if isinstance(result.status, RunStatus) else str(result.status),
            metrics=metrics,
            score=value,
            n_items=result.n_items,
            csv_manifest_uri=result.csv_manifest_uri,
            result=result,
        )
        entry = {
            "iteration": iteration,
            "run_id": run_id,
            "kind": kind,
            "suite_id": suite_id,
            "params": dict(params),
            "objective": value,
            "metrics": metrics,
            "status": "evaluated" if ok else "failed",
            "error": result.error,
            "error_kind": result.error_kind,
            "feasible": obj.feasible(metrics) if ok else False,
            "csv_uri": result.csv_manifest_uri,
            "derived_from": derived_from,
            "n_items": result.n_items,
        }
        await hooks.emit({
            "type": "run_evaluated" if kind == "search" else "validation_evaluated",
            "experiment_id": state["experiment_id"],
            "iteration": iteration,
            "run_id": run_id,
            "suite_id": suite_id,
            "params": params,
            "metrics": metrics,
            "objective": value,
            "feasible": entry["feasible"],
            "status": entry["status"],
            "error": result.error,
        })
        return entry

    # ------------------------------------------------------------------ nodes

    async def rag_retrieve(state: AlgOpsState) -> dict:
        try:
            ctx = hooks.retrieve_rag(
                project_id=state["project_id"],
                goal_text=state.get("goal_text", ""),
                target_metric=state["target_metric"],
                top_k=5,
                algorithm_id=state.get("algorithm_id"),
            )
        except Exception:  # noqa: BLE001
            ctx = None
        if not ctx:
            return {"rag_context": "", "rag_anchor_ids": []}
        return {"rag_context": ctx.get("text", ""), "rag_anchor_ids": ctx.get("anchor_ids", [])}

    async def pm_propose(state: AlgOpsState) -> dict:
        spec = _spec(state)
        obj = state_objective(state)
        by_key = spec.by_key()
        it = state.get("iteration", 0) + 1
        history = [h for h in state.get("history", []) if h.get("kind", "search") == "search"]
        k = max(1, int(state.get("batch_size") or 1))
        keys = [key for key in (state.get("search_keys") or []) if key in by_key]
        base = spec.effective()
        seen = {params_key(h["params"]) for h in history}
        cands: list[dict] = []
        rationale = ""
        if not history:
            cands.append(dict(base))  # iteration 1 = the repository's current defaults
            seen.add(params_key(base))
        best = best_entry(history, obj)
        center = dict(best["params"]) if best else dict(base)

        if deps.planner is not None and history and len(cands) < k:
            try:
                proposal = await deps.planner.propose(state, spec)
            except Exception:  # noqa: BLE001 — a flaky LLM never crashes a campaign
                proposal = None
            if proposal is not None:
                cand = dict(center)
                for key, value in proposal.as_changes().items():
                    if key in keys:
                        try:
                            cand[key] = by_key[key].coerce(value)
                        except ValueError:
                            continue
                h = params_key(cand)
                if cand != center and h not in seen:
                    cands.append(cand)
                    seen.add(h)
                    rationale = proposal.rationale or ""

        sstate = dict(state.get("search_state") or {})
        converged = False
        if len(cands) < k:
            pending = history + [{"params": c} for c in cands]
            more, sstate, conv = strategy_propose(
                state.get("strategy") or "coordinate", spec, keys, obj, pending, sstate,
                k - len(cands), seed=int(state.get("seed") or 0), base=base,
            )
            cands += more
            converged = conv and not cands
        return {
            "iteration": it,
            "proposed_batch": cands,
            "proposed_params": cands[0] if cands else center,
            "search_state": sstate,
            "converged": converged,
            "proposal_rationale": rationale,
        }

    async def developer(state: AlgOpsState) -> dict:
        # Optional HITL gate: pause for human approval BEFORE the batch is evaluated. The
        # interrupt re-runs this node on resume, so its only effect (current_batch) is idempotent.
        if state.get("require_approval") and state.get("proposed_batch"):
            from langgraph.types import interrupt

            interrupt({
                "type": "approval_required",
                "experiment_id": state["experiment_id"],
                "iteration": state["iteration"],
                "proposed_params": state["proposed_params"],
                "proposed_batch": state["proposed_batch"],
                "rationale": state.get("proposal_rationale", ""),
            })
        return {"current_batch": list(state.get("proposed_batch") or [])}

    async def run_eval(state: AlgOpsState) -> dict:
        batch = state.get("current_batch") or []
        if not batch:
            return {}
        obj = state_objective(state)
        derived = state.get("best_run_id") or state.get("last_run_id")
        entries = await asyncio.gather(*(
            _evaluate(state, params, suite_id=state.get("suite_id", "default"), kind="search",
                      derived_from=derived, iteration=state["iteration"])
            for params in batch
        ))
        ok = [e for e in entries if e["status"] == "evaluated"]
        focus = min(ok, key=lambda e: obj.rank(e["metrics"])) if ok else entries[0]
        return {
            "history": list(entries),
            "last_run_id": focus["run_id"],
            "last_metrics": focus["metrics"],
            "last_score": focus["objective"],
            "current_params": focus["params"],
            "last_csv_uri": focus.get("csv_uri"),
        }

    async def extract_csv(state: AlgOpsState) -> dict:
        if not state.get("current_batch"):
            return {}  # nothing evaluated this iteration (search converged) — keep the last analysis
        uri = state.get("last_csv_uri")
        text = hooks.fetch_text(uri) if uri else None
        if not text:
            return {"csv_drop_reasons": {}, "csv_n_items": 0, "csv_sensitivity": {}}
        manifest = load_manifest(text)
        if manifest is None:
            try:  # back-compat: a single decision CSV instead of a manifest
                agg = aggregate_decision_csv(text)
            except Exception:  # noqa: BLE001
                return {}
            return {"csv_drop_reasons": agg.get("drop_reasons", {}), "csv_n_items": agg.get("n_items", 0)}
        stage_texts: dict[str, str] = {}
        for stage in manifest.get("stages", []):
            try:
                stage_text = hooks.fetch_text(resolve_sibling_uri(uri, stage["csv"]))
            except (KeyError, TypeError, ValueError):
                continue  # malformed manifest entry — skip that stage
            if stage_text:
                stage_texts[stage["stage"]] = stage_text
        try:
            agg = aggregate_multistage(manifest, stage_texts, reason_map=state.get("reason_map") or {})
        except Exception:  # noqa: BLE001
            return {}
        return {
            "csv_drop_reasons": agg.get("drop_reasons", {}),
            "csv_n_items": agg.get("n_items", 0),
            "csv_sensitivity": agg.get("sensitivity", {}),
        }

    async def pandas_summarize(state: AlgOpsState) -> dict:
        entries = _iteration_entries(state)
        if not entries:
            return {}
        obj = state_objective(state)
        parent_params = state.get("best_params") or {}
        parent_metrics = state.get("best_metrics") or {}
        focus_md: str | None = None
        for entry in entries:
            is_focus = entry["run_id"] == state.get("last_run_id")
            md = None
            if is_focus and deps.planner is not None:
                try:
                    md = await deps.planner.summarize(state)
                except Exception:  # noqa: BLE001
                    md = None
            if not md:
                md = summarize_markdown(
                    project_name=state.get("project_name", "project"),
                    iteration_index=state["iteration"],
                    param_values=entry["params"],
                    metrics=entry["metrics"],
                    target_metric=obj.metric,
                    target_value=obj.target,
                    direction=obj.direction,
                    drop_reasons=state.get("csv_drop_reasons") if is_focus else None,
                    n_items=entry.get("n_items"),
                    objective=obj,
                    algorithm_id=state.get("algorithm_id"),
                    suite_id=entry.get("suite_id"),
                    commit_sha=state.get("commit_sha"),
                )
                if entry["status"] != "evaluated":
                    md += f"\n## 실패\n- {entry.get('error_kind')}: {entry.get('error')}\n"
            if is_focus:
                focus_md = md
            hooks.put_summary(entry["run_id"], md)
            if entry["status"] != "evaluated":
                continue
            changed = [k for k, v in entry["params"].items() if parent_params.get(k) != v] if parent_params else []
            deltas: dict[str, float] = {}
            if parent_metrics:
                names = {obj.metric, *[c.metric for c in obj.constraints], "fp_rate", "fn_rate", "f1"}
                for name in names:
                    if name in entry["metrics"] and name in parent_metrics:
                        deltas[name] = entry["metrics"][name] - parent_metrics[name]
            try:
                hooks.index_run_report(
                    run_id=entry["run_id"],
                    experiment_id=state["experiment_id"],
                    project_id=state["project_id"],
                    algorithm_id=state.get("algorithm_id", "default"),
                    iteration_index=state["iteration"],
                    goal_tag=state.get("goal_text", ""),
                    params=dict(entry["params"]),
                    params_changed=changed,
                    metric_deltas=deltas,
                    primary_metric=obj.metric,
                    primary_metric_value=entry["metrics"].get(obj.metric),
                    report_md=md,
                    derived_from=entry.get("derived_from") if parent_params else None,
                )
            except Exception:  # noqa: BLE001 — RAG indexing never breaks the loop
                pass
        return {"csv_summary_md": focus_md or ""}

    async def analyst(state: AlgOpsState) -> dict:
        entries = _iteration_entries(state)
        if not entries:
            return {}
        if deps.planner is not None:
            try:
                findings = await deps.planner.analyze(state)
            except Exception:  # noqa: BLE001
                findings = None
            if findings:
                return {"analyst_findings": findings}
        obj = state_objective(state)
        m = state.get("last_metrics") or {}
        ok = [e for e in entries if e["status"] == "evaluated"]
        if not ok:
            first = entries[0]
            return {"analyst_findings": f"iteration {state['iteration']}: 모든 후보 실패 — "
                                        f"{first.get('error_kind')}: {str(first.get('error'))[:300]}"}
        shown = [obj.metric, *[c.metric for c in obj.constraints]]
        shown += [n for n in ("precision", "recall", "f1") if n in m and n not in shown]
        parts = ", ".join(f"{n}={m[n]:.4f}" for n in shown if n in m)
        note = f"iteration {state['iteration']}: {parts}"
        if len(entries) > 1:
            note += f" (배치 {len(entries)}개 중 최선, 실패 {len(entries) - len(ok)}개)"
        if obj.constraints and not obj.feasible(m):
            note += " — 제약 미충족"
        return {"analyst_findings": note}

    async def pm_decide(state: AlgOpsState) -> dict:
        obj = state_objective(state)
        history = [h for h in state.get("history", []) if h.get("kind", "search") == "search"]
        entries = _iteration_entries(state)
        updates: dict[str, Any] = {}
        best = best_entry(history, obj)
        if best is not None and best["run_id"] != state.get("best_run_id"):
            updates.update(best_score=best["objective"], best_run_id=best["run_id"],
                           best_params=dict(best["params"]), best_metrics=dict(best["metrics"]))
        failed_iters = int(state.get("failed_iterations") or 0)
        if entries:
            failed_iters = failed_iters + 1 if all(e["status"] != "evaluated" for e in entries) else 0
        updates["failed_iterations"] = failed_iters
        best_metrics = (best or {}).get("metrics") or {}

        action: str | None = None
        reason: str | None = None
        if best is None and state.get("iteration", 0) >= 1 and entries:
            action, reason = "stop", "error"  # the baseline itself does not evaluate
        elif failed_iters >= MAX_FAILED_ITERATIONS:
            action, reason = "stop", "error"
        elif obj.reached(best_metrics):
            action, reason = "stop", "target_reached"
        elif deps.planner is not None:
            try:  # LLM decides continue/stop; caps + target stay enforced by the router.
                decision = await deps.planner.decide(state)
            except Exception:  # noqa: BLE001
                decision = None
            if decision is not None and decision.action in ("continue", "stop"):
                action = decision.action
                # The LLM may only *choose* to stop; it cannot claim the target was reached or
                # that the run failed — those verdicts are computed above, deterministically.
                if action == "stop":
                    reason = "converged" if decision.stop_reason == "converged" else "pm_stop"
        if action is None:
            if state.get("converged"):
                action, reason = "stop", "converged"
            else:
                action, reason = "continue", None

        value = best_metrics.get(obj.metric)
        rationale = (
            f"best {obj.metric}={value:.4f} ({obj.describe()})" if value is not None
            else f"no evaluated run yet ({obj.describe()})"
        )
        if state.get("proposal_rationale"):
            rationale += f" · 제안 근거: {state['proposal_rationale']}"
        hooks.on_iteration(
            iteration=state["iteration"],
            analyst_findings=state.get("analyst_findings"),
            pm_action=action,
            stop_reason=reason,
            rationale=rationale,
            proposed_run_id=state.get("last_run_id"),
        )
        await hooks.emit({
            "type": "iteration_decided",
            "experiment_id": state["experiment_id"],
            "iteration": state["iteration"],
            "pm_action": action,
            "stop_reason": reason,
            "best_score": updates.get("best_score", state.get("best_score")),
            "best_run_id": updates.get("best_run_id", state.get("best_run_id")),
        })
        updates.update(pm_action=action, stop_reason=reason)
        return updates

    async def validate(state: AlgOpsState) -> dict:
        suites = [s for s in (state.get("validation_suite_ids") or []) if s]
        best_params = state.get("best_params")
        if not suites or not best_params:
            return {"validation": {}}
        obj = state_objective(state)
        base = _spec(state).effective()
        it = state.get("iteration", 0)
        jobs = []
        for sid in suites:
            jobs.append(_evaluate(state, best_params, suite_id=sid, kind="validation",
                                  derived_from=state.get("best_run_id"), iteration=it))
            if params_key(base) != params_key(best_params):
                jobs.append(_evaluate(state, base, suite_id=sid, kind="validation",
                                      derived_from=None, iteration=it))
        entries = await asyncio.gather(*jobs)
        validation: dict[str, Any] = {}
        for sid in suites:
            mine = [e for e in entries if e["suite_id"] == sid]
            best_e = next((e for e in mine if params_key(e["params"]) == params_key(best_params)), None)
            base_e = next((e for e in mine if params_key(e["params"]) == params_key(base)), best_e)

            def pack(e: dict | None) -> dict | None:
                if e is None:
                    return None
                return {"run_id": e["run_id"], "status": e["status"], "metrics": e["metrics"],
                        "objective": e["objective"], "feasible": e["feasible"],
                        "reached": obj.reached(e["metrics"]), "error": e.get("error")}

            b, d = pack(best_e), pack(base_e)
            improved = None
            if b and d and b["status"] == d["status"] == "evaluated":
                improved = obj.rank(b["metrics"]) <= obj.rank(d["metrics"])
            validation[sid] = {"best": b, "baseline": d, "improved": improved}
        return {"validation": validation, "history": [dict(e) for e in entries]}

    async def finalize(state: AlgOpsState) -> dict:
        obj = state_objective(state)
        params = state.get("best_params") or state.get("current_params") or {}
        metrics = dict(state.get("best_metrics") or state.get("last_metrics") or {})
        winning = state.get("best_run_id")
        reason = state.get("stop_reason")
        if reason is None:
            reason = "max_iteration" if state.get("iteration", 0) >= state["max_iteration"] else "pm_stop"
        defaults = _spec(state).effective()
        changed = {k: {"old": defaults.get(k), "new": v} for k, v in params.items() if defaults.get(k) != v}

        report = summarize_markdown(
            project_name=state.get("project_name", "project"),
            iteration_index=state.get("iteration", 0),
            param_values=params,
            metrics=metrics,
            target_metric=obj.metric,
            target_value=obj.target,
            direction=obj.direction,
            objective=obj,
            algorithm_id=state.get("algorithm_id"),
            suite_id=state.get("suite_id"),
            commit_sha=state.get("commit_sha"),
        )
        extra: list[str] = []
        if changed:
            extra += ["", "## 기본값 대비 변경", "| 키 | 기존 | 최적 |", "| --- | --- | --- |"]
            extra += [f"| `{k}` | {c['old']} | {c['new']} |" for k, c in changed.items()]
        validation = state.get("validation") or {}
        if validation:
            extra += ["", "## 검증 스위트 (held-out)", f"| 스위트 | 최적 {obj.metric} | 기준 {obj.metric} | 개선 |",
                      "| --- | --- | --- | --- |"]
            for sid, v in validation.items():
                b = (v.get("best") or {}).get("objective")
                d = (v.get("baseline") or {}).get("objective")
                fb = f"{b:.4f}" if isinstance(b, (int, float)) else "-"
                fd = f"{d:.4f}" if isinstance(d, (int, float)) else "-"
                mark = {True: "✓", False: "✗", None: "-"}[v.get("improved")]
                extra.append(f"| {sid} | {fb} | {fd} | {mark} |")
        report = f"# 최종 결과 (종료 사유: {reason})\n\n" + report + ("\n".join(extra) + "\n" if extra else "")

        # Off the event loop: persisting + SMTP/Slack delivery must not stall other campaigns.
        refs = await asyncio.to_thread(
            hooks.on_finalize,
            winning_run_id=winning,
            params=params,
            metrics=metrics,
            stop_reason=reason,
            report_md=report,
            validation=validation,
            changed_params=changed,
        )
        await hooks.emit({
            "type": "finalized",
            "experiment_id": state["experiment_id"],
            "winning_run_id": winning,
            "stop_reason": reason,
            "params": params,
            "metrics": metrics,
            "changed_params": changed,
            "validation": validation,
            "notify_refs": refs,
        })
        return {
            "final_params": params,
            "final_metrics": metrics,
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
        "validate": validate,
        "finalize": finalize,
    }


def route_after_pm(state: AlgOpsState) -> str:
    """Stop conditions enforced in the router (defense in depth), not by the agent."""
    if state.get("pm_action") == "stop":
        return "finalize"
    if state.get("iteration", 0) >= state["max_iteration"]:
        return "finalize"
    best = state.get("best_metrics") or state.get("last_metrics")
    if best and state_objective(state).reached(best):
        return "finalize"
    return "continue"


def make_default_deps() -> LoopDeps:
    """Deps with a null executor placeholder — callers must supply a real executor."""
    raise RuntimeError("supply LoopDeps(executor=..., hooks=...) explicitly")
