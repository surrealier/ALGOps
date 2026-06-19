"""Pluggable embeddings. HashingEmbedder is deterministic and dependency-free (dev/test);
FastEmbedEmbedder (optional 'rag-models' extra) serves BGE-M3-class dense vectors in prod.
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Protocol

_TOKEN = re.compile(r"[a-z0-9_.]+|[가-힣]+")


def tokens(text: str) -> list[str]:
    return _TOKEN.findall((text or "").lower())


class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...


class HashingEmbedder:
    """Deterministic hashed bag-of-tokens vector (L2-normalized). No model, no network."""

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for tok in tokens(text):
            h = int(hashlib.md5(tok.encode("utf-8")).hexdigest(), 16)
            vec[h % self.dim] += 1.0
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]


class FastEmbedEmbedder:  # pragma: no cover - requires 'rag-models' extra
    def __init__(self, model: str = "BAAI/bge-small-en-v1.5") -> None:
        from fastembed import TextEmbedding

        self._model = TextEmbedding(model_name=model)
        self.dim = 384

    def embed(self, text: str) -> list[float]:
        return list(next(self._model.embed([text])))


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))  # both are L2-normalized
