"""The RAG store interface used by the loop (via hooks)."""

from __future__ import annotations

from typing import Protocol

from .models import RagContext, RagQuery, RunReport


class RagStore(Protocol):
    def index_run(self, report: RunReport) -> None: ...
    def retrieve(self, query: RagQuery) -> RagContext: ...
