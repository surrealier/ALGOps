"""OpenAI-compatible LLM client construction + provider probe (vLLM / Ollama / external).

Everything reaches the LLM through the ``/v1`` API, so "external vLLM", "external Ollama",
and "master-spun vLLM" are the same code path — only ``LLMConfig`` differs. ``langchain-openai``
is an optional extra (``uv sync --extra llm``); it is imported lazily so the M0 dev path,
which never calls an LLM, needs no extra install.
"""

from __future__ import annotations

from typing import Any

import httpx

from algops.core import LLMConfig


def build_chat(cfg: LLMConfig, *, temperature: float = 0.0) -> Any | None:
    """Return a LangChain chat model for the configured endpoint, or None when mode='none'."""
    if cfg.mode == "none" or not cfg.base_url:
        return None
    try:
        from langchain_openai import ChatOpenAI
    except ImportError as exc:  # pragma: no cover - exercised only when extra missing
        raise RuntimeError(
            "LLM mode requires the 'llm' extra: uv sync --extra llm"
        ) from exc
    return ChatOpenAI(
        base_url=cfg.base_url,
        model=cfg.model or "default",
        api_key=cfg.api_key or "EMPTY",  # vLLM/Ollama accept any non-empty key
        temperature=temperature,
        timeout=120,
        max_retries=2,
    )


async def probe_provider(cfg: LLMConfig) -> dict:
    """Liveness/identity check against the OpenAI-compatible endpoint (GET /v1/models)."""
    if not cfg.base_url:
        return {"ok": False, "error": "no base_url"}
    url = cfg.base_url.rstrip("/") + "/models"
    headers = {"Authorization": f"Bearer {cfg.api_key or 'EMPTY'}"}
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(url, headers=headers)
            resp.raise_for_status()
            data = resp.json()
            models = [m.get("id") for m in data.get("data", [])]
            return {"ok": True, "models": models, "configured_model": cfg.model}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
