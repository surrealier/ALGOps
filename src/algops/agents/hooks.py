"""Side-effect hooks the loop calls (persistence, artifacts, progress, notification).

Keeping these behind a protocol lets the loop stay pure: the smoke harness uses ``NullHooks``
(or a printing subclass) while the master wires a DB/artifact/WS/notify implementation.
"""

from __future__ import annotations

from typing import Any, Protocol

from algops.core import Metrics


class LoopHooks(Protocol):
    async def emit(self, event: dict[str, Any]) -> None:
        """Push a progress event (WS to the browser / log)."""

    def on_run_created(
        self, *, run_id: str, iteration: int, params: dict, derived_from: str | None
    ) -> None: ...

    def on_run_result(
        self,
        *,
        run_id: str,
        status: str,
        metrics: Metrics | None,
        score: float | None,
        n_items: int | None,
        csv_manifest_uri: str | None,
    ) -> None: ...

    def put_summary(self, run_id: str, md: str) -> str | None:
        """Persist a run summary artifact, returning its uri (or None)."""

    def fetch_text(self, uri: str | None) -> str | None:
        """Read back an artifact (e.g. the slave-produced CSV) as text."""

    def on_iteration(
        self,
        *,
        iteration: int,
        analyst_findings: str | None,
        pm_action: str | None,
        stop_reason: str | None,
        rationale: str | None,
        proposed_run_id: str | None,
    ) -> None: ...

    def on_finalize(
        self,
        *,
        winning_run_id: str | None,
        params: dict,
        metrics: Metrics | None,
        stop_reason: str | None,
        report_md: str,
    ) -> dict[str, Any]:
        """Persist final result, set experiment status, fire notification. Returns refs."""

    def index_run_report(self, **fields: Any) -> None:
        """Index a run's report into the RAG stores (Qdrant doc + Neo4j graph)."""

    def retrieve_rag(
        self, *, project_id: str, goal_text: str, target_metric: str, top_k: int
    ) -> dict[str, Any]:
        """Retrieve prior-experiment context. Returns {'text': str, 'anchor_ids': list}."""


class NullHooks:
    """No-op hooks (loop runs with no persistence/notification)."""

    async def emit(self, event: dict[str, Any]) -> None:
        return None

    def on_run_created(self, **_: Any) -> None:
        return None

    def on_run_result(self, **_: Any) -> None:
        return None

    def put_summary(self, run_id: str, md: str) -> str | None:
        return None

    def fetch_text(self, uri: str | None) -> str | None:
        return None

    def on_iteration(self, **_: Any) -> None:
        return None

    def on_finalize(self, **_: Any) -> dict[str, Any]:
        return {}

    def index_run_report(self, **_: Any) -> None:
        return None

    def retrieve_rag(self, **_: Any) -> dict[str, Any]:
        return {"text": "", "anchor_ids": []}
