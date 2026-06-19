"""DbHooks — the master's implementation of the loop's side-effect hooks.

Persists runs/iterations/final results to the tracking DB, stores artifacts, streams progress
to the browser over WebSocket, and fires the deterministic completion notification.
"""

from __future__ import annotations

import os
from typing import Any

from algops.core import MasterSettings, Metrics
from algops.notify import notify_experiment_complete
from algops.rag import RagQuery, RunReport
from algops.tracking import ArtifactStore, get_session, repo
from algops.tracking.artifacts import SCHEME


class DbHooks:
    def __init__(
        self,
        *,
        experiment_id: str,
        project_id: str,
        project_name: str,
        experiment_name: str,
        goal_text: str,
        target_metric: str,
        target_value: float,
        direction: str,
        artifact_store: ArtifactStore,
        ws_broker,
        settings: MasterSettings,
        rag_store=None,
    ) -> None:
        self.experiment_id = experiment_id
        self.project_id = project_id
        self.project_name = project_name
        self.experiment_name = experiment_name
        self.goal_text = goal_text
        self.target_metric = target_metric
        self.target_value = target_value
        self.direction = direction
        self.store = artifact_store
        self.ws = ws_broker
        self.settings = settings
        self.rag = rag_store

    async def emit(self, event: dict[str, Any]) -> None:
        await self.ws.publish(self.experiment_id, event)

    def on_run_created(self, *, run_id, iteration, params, derived_from) -> None:
        with get_session() as s:
            repo.create_run(
                s,
                run_id=run_id,
                experiment_id=self.experiment_id,
                project_id=self.project_id,
                iteration_index=iteration,
                param_values=params,
                derived_from_run=derived_from,
            )

    def on_run_result(self, *, run_id, status, metrics, score, n_items, csv_manifest_uri) -> None:
        with get_session() as s:
            repo.record_run_result(
                s,
                run_id=run_id,
                status=status,
                metrics=metrics,
                score=score,
                n_items=n_items,
                csv_manifest_uri=csv_manifest_uri,
            )

    def put_summary(self, run_id: str, md: str) -> str | None:
        uri = self.store.put_text(f"runs/{run_id}/summary.md", md)
        with get_session() as s:
            run = repo.get_run(s, run_id)
            if run is not None:
                run.summary_md_uri = uri
        return uri

    def fetch_text(self, uri: str | None) -> str | None:
        if not uri:
            return None
        if uri.startswith(SCHEME):
            try:
                return self.store.get_text(uri)
            except Exception:
                return None
        if os.path.exists(uri):
            with open(uri, encoding="utf-8") as fh:
                return fh.read()
        return None

    def on_iteration(
        self, *, iteration, analyst_findings, pm_action, stop_reason, rationale, proposed_run_id
    ) -> None:
        with get_session() as s:
            repo.create_iteration(
                s,
                experiment_id=self.experiment_id,
                iteration_index=iteration,
                analyst_findings=analyst_findings,
                pm_action=pm_action,
                stop_reason=stop_reason,
                rationale=rationale,
                proposed_run_id=proposed_run_id,
            )

    def on_finalize(
        self, *, winning_run_id, params, metrics: Metrics | None, stop_reason, report_md
    ) -> dict[str, Any]:
        report_uri = self.store.put_text(f"experiments/{self.experiment_id}/report.md", report_md)
        refs = notify_experiment_complete(
            self.settings,
            dedup_key=self.experiment_id,
            project_name=self.project_name,
            experiment_name=self.experiment_name,
            goal_text=self.goal_text,
            stop_reason=stop_reason,
            optimal_params=params,
            metrics=metrics,
            target_metric=self.target_metric,
            target_value=self.target_value,
            direction=self.direction,
        )
        with get_session() as s:
            repo.upsert_final_result(
                s,
                experiment_id=self.experiment_id,
                winning_run_id=winning_run_id,
                optimal_params=params,
                metrics=metrics,
                stop_reason=stop_reason,
                report_md_uri=report_uri,
                notified=True,
                notify_refs=refs,
            )
            if winning_run_id:
                repo.set_best_run(s, self.experiment_id, winning_run_id)
        return refs

    def index_run_report(self, **fields: Any) -> None:
        if self.rag is None:
            return
        try:
            self.rag.index_run(RunReport(**fields))
        except Exception:  # RAG indexing failure must never break the loop
            pass

    def retrieve_rag(
        self, *, project_id: str, goal_text: str, target_metric: str, top_k: int
    ) -> dict[str, Any]:
        if self.rag is None:
            return {"text": "", "anchor_ids": []}
        try:
            ctx = self.rag.retrieve(
                RagQuery(
                    project_id=project_id, goal_text=goal_text,
                    target_metric=target_metric, top_k=top_k,
                )
            )
            return {"text": ctx.text, "anchor_ids": ctx.anchor_run_ids}
        except Exception:
            return {"text": "", "anchor_ids": []}
