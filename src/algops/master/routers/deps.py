"""Shared FastAPI dependencies (slave bearer auth, header parsing)."""

from __future__ import annotations

from fastapi import Header, HTTPException

from algops.tracking import get_session, repo

# Master-side long-poll hold time for slave next-command requests.
LONG_POLL_S = 25.0


def _bearer(authorization: str | None) -> str:
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    return authorization.split(" ", 1)[1].strip()


def require_slave(slave_id: str, authorization: str | None = Header(default=None)) -> str:
    """Verify the project-scoped slave bearer token matches the slave in the path."""
    token = _bearer(authorization)
    with get_session() as s:
        if not repo.verify_slave_token(s, slave_id, token):
            raise HTTPException(status_code=401, detail="invalid slave token")
    return slave_id
