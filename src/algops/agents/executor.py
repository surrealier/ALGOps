"""How the loop asks for a parameter set to be evaluated over the test set.

The loop never imports a target project. It hands a ``RunRequest`` to a ``RunExecutor`` and
gets a ``RunResult`` back. Two implementations exist:

  * ``LocalRunExecutor``  — calls a sync run-adapter in-process (smoke tests, single-host dev).
  * ``CommandBusRunExecutor`` (in algops.master) — enqueues a command the slave long-polls for,
    then awaits the result the slave POSTs back. This is the real NAT-friendly distributed path.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Protocol

from algops.core import RunRequest, RunResult


class RunExecutor(Protocol):
    async def run(self, req: RunRequest) -> RunResult: ...


class LocalRunExecutor:
    """Wrap a synchronous ``run(RunRequest) -> RunResult`` callable as an async executor."""

    def __init__(self, run_fn: Callable[[RunRequest], RunResult]) -> None:
        self._fn = run_fn

    async def run(self, req: RunRequest) -> RunResult:
        return await asyncio.to_thread(self._fn, req)
