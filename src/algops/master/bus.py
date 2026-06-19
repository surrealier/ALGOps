"""In-process command bus for the slave-initiated (NAT-friendly) run dispatch.

The experiment loop calls ``submit_run`` and awaits the result; the slave long-polls
``next_command`` and POSTs the outcome to ``deliver_result``. In dev the API and the loop
share one event loop so an in-memory bus suffices. Prod (separate Celery worker) swaps this
for a Redis-backed bus + result store — same interface (M5).
"""

from __future__ import annotations

import asyncio

from algops.core import CommandKind, NextCommandResponse, RunRequest, RunResult, new_id


class CommandBus:
    def __init__(self) -> None:
        self._pending: dict[str, list[NextCommandResponse]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._results: dict[str, asyncio.Future] = {}

    def _event(self, slave_id: str) -> asyncio.Event:
        return self._events.setdefault(slave_id, asyncio.Event())

    async def submit_run(self, slave_id: str, req: RunRequest, timeout: float) -> RunResult:
        command_id = new_id()
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._results[command_id] = fut
        self._pending.setdefault(slave_id, []).append(
            NextCommandResponse(kind=CommandKind.run, command_id=command_id, run_spec=req)
        )
        self._event(slave_id).set()
        try:
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._results.pop(command_id, None)

    async def next_command(self, slave_id: str, timeout: float) -> NextCommandResponse:
        pending = self._pending.setdefault(slave_id, [])
        event = self._event(slave_id)
        if not pending:
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError:
                return NextCommandResponse(kind=CommandKind.noop)
        if pending:
            cmd = pending.pop(0)
            if not pending:
                event.clear()
            return cmd
        event.clear()
        return NextCommandResponse(kind=CommandKind.noop)

    async def deliver_result(self, command_id: str, result: RunResult) -> bool:
        fut = self._results.get(command_id)
        if fut is not None and not fut.done():
            fut.set_result(result)
            return True
        return False


class RedisCommandBus:  # pragma: no cover - requires a running Redis (prod cross-process path)
    """Redis-backed bus so a separate Celery worker can dispatch runs to slaves and collect
    results posted via the API process. Lists + BLPOP give at-least-once delivery + blocking wait.
    """

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._r = redis.from_url(url)

    async def submit_run(self, slave_id: str, req: RunRequest, timeout: float) -> RunResult:
        command_id = new_id()
        cmd = NextCommandResponse(kind=CommandKind.run, command_id=command_id, run_spec=req)
        await self._r.rpush(f"algops:cmds:{slave_id}", cmd.model_dump_json())
        popped = await self._r.blpop(f"algops:res:{command_id}", timeout=int(timeout))
        if popped is None:
            raise asyncio.TimeoutError(f"slave {slave_id} did not return run {req.run_id} in time")
        return RunResult.model_validate_json(popped[1])

    async def next_command(self, slave_id: str, timeout: float) -> NextCommandResponse:
        popped = await self._r.blpop(f"algops:cmds:{slave_id}", timeout=int(timeout))
        if popped is None:
            return NextCommandResponse(kind=CommandKind.noop)
        return NextCommandResponse.model_validate_json(popped[1])

    async def deliver_result(self, command_id: str, result: RunResult) -> bool:
        await self._r.rpush(f"algops:res:{command_id}", result.model_dump_json())
        return True


def get_command_bus(settings):
    """memory (in-process dev) or redis (prod cross-process Celery worker)."""
    if getattr(settings, "command_bus", "memory") == "redis":
        return RedisCommandBus(settings.celery_broker_url)
    return CommandBus()
