"""algops.rag — dual-store RAG (Qdrant + Neo4j) with a dependency-free in-memory fallback (M3)."""

from __future__ import annotations

from algops.core import MasterSettings

from .memory import InMemoryRagStore
from .models import ParamHint, RagContext, RagQuery, RunReport
from .store import RagStore


def get_rag_store(settings: MasterSettings | None = None) -> RagStore:
    settings = settings or MasterSettings()
    if settings.rag_backend == "qdrant_neo4j":
        from .qdrant_neo4j import QdrantNeo4jRagStore

        return QdrantNeo4jRagStore(settings)
    return InMemoryRagStore()


__all__ = [
    "get_rag_store",
    "RagStore",
    "InMemoryRagStore",
    "RunReport",
    "RagQuery",
    "RagContext",
    "ParamHint",
]
