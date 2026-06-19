"""Pluggable reranker. NoopReranker keeps first-stage order (dev/test); BgeReranker (optional
'rag-models' extra) is the cross-encoder used in prod. First-stage recall bounds quality, so the
reranker is a refinement, not a fix for missed retrieval (plan risk #7)."""

from __future__ import annotations

from typing import Protocol


class Reranker(Protocol):
    def rerank(self, query: str, docs: list[tuple[str, str]], top_k: int) -> list[str]:
        """Given (id, text) candidates, return the top_k ids best matching the query."""


class NoopReranker:
    def rerank(self, query: str, docs: list[tuple[str, str]], top_k: int) -> list[str]:
        return [doc_id for doc_id, _ in docs[:top_k]]


class BgeReranker:  # pragma: no cover - requires 'rag-models' extra (FlagEmbedding/torch)
    def __init__(self, model: str = "BAAI/bge-reranker-v2-m3") -> None:
        from FlagEmbedding import FlagReranker

        self._reranker = FlagReranker(model, use_fp16=True)

    def rerank(self, query: str, docs: list[tuple[str, str]], top_k: int) -> list[str]:
        scores = self._reranker.compute_score([[query, text] for _, text in docs], normalize=True)
        ranked = sorted(zip(docs, scores), key=lambda x: -x[1])
        return [doc_id for (doc_id, _), _ in ranked[:top_k]]
