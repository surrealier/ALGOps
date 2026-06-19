"""LLM endpoint liveness probe (so the UI can confirm the configured vLLM/Ollama is reachable)."""

from __future__ import annotations

from fastapi import APIRouter, Request

from algops.llm import probe_provider

router = APIRouter(prefix="/api/v1/llm", tags=["llm"])


@router.get("/probe")
async def probe(request: Request) -> dict:
    return await probe_provider(request.app.state.settings.llm)
