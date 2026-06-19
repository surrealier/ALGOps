"""Read-only artifact fetch (for the UI to render run summaries / final reports)."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse

router = APIRouter(prefix="/api/v1/artifacts", tags=["artifacts"])


@router.get("", response_class=PlainTextResponse)
def get_artifact(uri: str, request: Request) -> str:
    try:
        return request.app.state.store.get_text(uri)
    except Exception:
        raise HTTPException(404, "artifact not found")
