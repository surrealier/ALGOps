"""algops.slave — the runner agent: register · heartbeat · long-poll · run/sync/promote · upload.

Outbound-only. A v2 runner serves any number of repositories (git checkouts per commit + local
paths). ``runner.py`` keeps the v1 in-process helpers for backwards compatibility.
"""

from __future__ import annotations

from .client import MasterClient
from .poller import Runner, build_engine, local_project_map, run_slave
from .runner import execute_run, resolve_project

__all__ = [
    "MasterClient",
    "Runner",
    "build_engine",
    "local_project_map",
    "run_slave",
    "execute_run",
    "resolve_project",
]
