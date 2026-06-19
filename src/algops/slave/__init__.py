"""algops.slave — per-project agent: register, long-poll, run, upload (outbound only)."""

from __future__ import annotations

from .client import MasterClient
from .poller import run_slave
from .runner import execute_run, resolve_project

__all__ = ["MasterClient", "run_slave", "execute_run", "resolve_project"]
