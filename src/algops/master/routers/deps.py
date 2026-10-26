"""Shared FastAPI dependencies: slave bearer auth, optional API-token auth, the master context."""

from __future__ import annotations

import hmac

from fastapi import Header, HTTPException, Request

from algops.tracking import get_session, repo

from ..context import MasterContext

# Master-side long-poll hold time for slave next-command requests.
LONG_POLL_S = 25.0


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def require_slave(slave_id: str, authorization: str | None = Header(default=None)) -> str:
    """Verify the runner bearer token matches the slave in the path."""
    token = _bearer(authorization)
    with get_session() as s:
        if not repo.verify_slave_token(s, slave_id, token):
            raise HTTPException(status_code=401, detail="invalid slave token")
    return slave_id


def require_api(request: Request, authorization: str | None = Header(default=None)) -> None:
    """Mutating endpoints need ``Authorization: Bearer <api_token>`` when a token is configured."""
    expected = request.app.state.settings.api_token
    if not expected:
        return
    token = _bearer(authorization)
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=401, detail="invalid API token")


def get_ctx(request: Request) -> MasterContext:
    return request.app.state.ctx
