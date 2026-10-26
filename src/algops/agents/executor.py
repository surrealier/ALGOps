"""How the loop asks for a parameter set to be evaluated.

The loop never imports a target project. It hands a ``RunRequest`` to a ``RunExecutor`` and
gets a ``RunResult`` back. Implementations:

  * ``LocalRunExecutor``       — calls a sync ``run(RunRequest) -> RunResult`` in a thread
                                  (tests, smoke harness).
  * ``EngineRunExecutor``      — runs through the local :class:`algops.runner.RunnerEngine`
                                  (the real protocol, no master).
  * ``DispatcherRunExecutor``  — (algops.master) pins repo/sha and dispatches to a runner.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any, Protocol

from algops.core import RepoRef, RunRequest, RunResult


class RunExecutor(Protocol):
    async def run(self, req: RunRequest) -> RunResult: ...


class LocalRunExecutor:
    """Wrap a synchronous ``run(RunRequest) -> RunResult`` callable as an async executor."""

    def __init__(self, run_fn: Callable[[RunRequest], RunResult]) -> None:
        self._fn = run_fn

    async def run(self, req: RunRequest) -> RunResult:
        return await asyncio.to_thread(self._fn, req)


class EngineRunExecutor:
    """Evaluate through a RunnerEngine serving a local project (no master involved)."""

    def __init__(self, engine: Any, project_name: str) -> None:
        self.engine = engine
        self.project_name = project_name

    async def run(self, req: RunRequest) -> RunResult:
        req = req.model_copy(update={"repo": RepoRef(kind="local", name=self.project_name)})
        outcome = await self.engine.run(req)
        return outcome.result
