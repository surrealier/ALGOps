"""Command bus for slave-initiated (NAT-friendly) dispatch: run / sync / promote commands.

The master enqueues a command for a specific runner; the runner long-polls ``next_command``,
executes, and POSTs a ``CommandResultUpload`` that ``deliver`` hands back to the waiter.

* ``CommandBus``       in-process (API + loop share one event loop) — dev / single process.
* ``RedisCommandBus``  lists + BLPOP — the API process and Celery workers share it in prod.

Waiting is sliced (``wait(command_id, timeout)`` returns None when a slice expires) so the
dispatcher can notice a runner that went offline mid-run and re-dispatch elsewhere.
"""

from __future__ import annotations

import asyncio

from algops.core import (
    CommandKind,
    CommandResultUpload,
    NextCommandResponse,
    RunRequest,
    RunResult,
    new_id,
)


class CommandBus:
    def __init__(self) -> None:
        self._pending: dict[str, list[NextCommandResponse]] = {}
        self._events: dict[str, asyncio.Event] = {}
        self._results: dict[str, asyncio.Future] = {}
        self._owner: dict[str, str] = {}  # command_id -> slave_id (in flight)
        self._picked: set[str] = set()  # commands a runner has taken off its queue

    def _event(self, slave_id: str) -> asyncio.Event:
        return self._events.setdefault(slave_id, asyncio.Event())

    def _future(self, command_id: str) -> asyncio.Future:
        fut = self._results.get(command_id)
        if fut is None:
            fut = asyncio.get_running_loop().create_future()
            self._results[command_id] = fut
        return fut

    async def enqueue(self, slave_id: str, cmd: NextCommandResponse) -> str:
        if cmd.command_id is None:
            cmd = cmd.model_copy(update={"command_id": new_id()})
        self._future(cmd.command_id)
        self._owner[cmd.command_id] = slave_id
        self._pending.setdefault(slave_id, []).append(cmd)
        self._event(slave_id).set()
        return cmd.command_id

    async def wait(self, command_id: str, timeout: float) -> CommandResultUpload | None:
        fut = self._results.get(command_id)
        if fut is None:  # unknown or already forgotten — never resurrect it
            return None
        try:
            return await asyncio.wait_for(asyncio.shield(fut), timeout)
        except asyncio.TimeoutError:
            return None

    async def picked(self, command_id: str) -> bool:
        """Has a runner taken this command off its queue (i.e. started executing it)?"""
        return command_id in self._picked

    async def forget(self, command_id: str) -> None:
        """Drop a command (queued or in flight) — its late result, if any, is ignored."""
        slave_id = self._owner.pop(command_id, None)
        self._picked.discard(command_id)
        if slave_id is not None:
            queue = [c for c in self._pending.get(slave_id, []) if c.command_id != command_id]
            if queue:
                self._pending[slave_id] = queue
            else:
                self._pending.pop(slave_id, None)
                ev = self._events.get(slave_id)
                if ev is not None:
                    ev.clear()
        fut = self._results.pop(command_id, None)
        if fut is not None and not fut.done():
            fut.cancel()

    def inflight(self, slave_id: str) -> int:
        return sum(1 for owner in self._owner.values() if owner == slave_id)

    async def next_command(self, slave_id: str, timeout: float) -> NextCommandResponse:
        pending = self._pending.setdefault(slave_id, [])
        event = self._event(slave_id)
        if not pending:
            try:
                await asyncio.wait_for(event.wait(), timeout)
            except asyncio.TimeoutError:
                return NextCommandResponse(kind=CommandKind.noop)
        pending = self._pending.get(slave_id, [])
        if pending:
            cmd = pending.pop(0)
            if not pending:
                event.clear()
            if cmd.command_id:
                self._picked.add(cmd.command_id)
            return cmd
        event.clear()
        return NextCommandResponse(kind=CommandKind.noop)

    async def deliver(self, upload: CommandResultUpload) -> bool:
        fut = self._results.get(upload.command_id)
        self._owner.pop(upload.command_id, None)
        self._picked.discard(upload.command_id)
        if fut is not None and not fut.done():
            fut.set_result(upload)
            return True
        return False

    # ---- v1 convenience (kept for callers/tests that dispatch a run to a known slave) ----

    async def submit_run(self, slave_id: str, req: RunRequest, timeout: float) -> RunResult:
        cid = await self.enqueue(slave_id, NextCommandResponse(kind=CommandKind.run, run_spec=req))
        try:
            up = await self.wait(cid, timeout)
            if up is None:
                raise asyncio.TimeoutError(f"slave {slave_id} did not return run {req.run_id} in time")
            if up.result is None:
                raise RuntimeError("slave returned no run result")
            return up.result
        finally:
            await self.forget(cid)

    async def deliver_result(self, command_id: str, result: RunResult) -> bool:
        return await self.deliver(CommandResultUpload(command_id=command_id, result=result))


class RedisCommandBus:  # pragma: no cover - requires a running Redis (prod cross-process path)
    """Redis-backed bus so Celery workers can dispatch commands collected by the API process."""

    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._r = redis.from_url(url)
        self._local_owner: dict[str, str] = {}

    async def enqueue(self, slave_id: str, cmd: NextCommandResponse) -> str:
        if cmd.command_id is None:
            cmd = cmd.model_copy(update={"command_id": new_id()})
        self._local_owner[cmd.command_id] = slave_id
        await self._r.rpush(f"algops:cmds:{slave_id}", cmd.model_dump_json())
        return cmd.command_id

    async def wait(self, command_id: str, timeout: float) -> CommandResultUpload | None:
        popped = await self._r.blpop(f"algops:res:{command_id}", timeout=max(1, int(timeout)))
        if popped is None:
            return None
        self._local_owner.pop(command_id, None)
        return CommandResultUpload.model_validate_json(popped[1])

    async def picked(self, command_id: str) -> bool:
        return bool(await self._r.exists(f"algops:picked:{command_id}"))

    async def forget(self, command_id: str) -> None:
        slave_id = self._local_owner.pop(command_id, None)
        await self._r.delete(f"algops:picked:{command_id}")
        if slave_id is None:
            return
        key = f"algops:cmds:{slave_id}"
        for raw in await self._r.lrange(key, 0, -1):
            try:
                if NextCommandResponse.model_validate_json(raw).command_id == command_id:
                    await self._r.lrem(key, 1, raw)
            except Exception:  # noqa: BLE001
                continue
        await self._r.delete(f"algops:res:{command_id}")

    def inflight(self, slave_id: str) -> int:
        return sum(1 for owner in self._local_owner.values() if owner == slave_id)

    async def next_command(self, slave_id: str, timeout: float) -> NextCommandResponse:
        popped = await self._r.blpop(f"algops:cmds:{slave_id}", timeout=max(1, int(timeout)))
        if popped is None:
            return NextCommandResponse(kind=CommandKind.noop)
        cmd = NextCommandResponse.model_validate_json(popped[1])
        if cmd.command_id:
            await self._r.set(f"algops:picked:{cmd.command_id}", "1", ex=7 * 86400)
        return cmd

    async def deliver(self, upload: CommandResultUpload) -> bool:
        key = f"algops:res:{upload.command_id}"
        await self._r.rpush(key, upload.model_dump_json())
        await self._r.expire(key, 3600)
        return True

    async def submit_run(self, slave_id: str, req: RunRequest, timeout: float) -> RunResult:
        cid = await self.enqueue(slave_id, NextCommandResponse(kind=CommandKind.run, run_spec=req))
        up = await self.wait(cid, timeout)
        if up is None or up.result is None:
            raise asyncio.TimeoutError(f"slave {slave_id} did not return run {req.run_id} in time")
        return up.result

    async def deliver_result(self, command_id: str, result: RunResult) -> bool:
        return await self.deliver(CommandResultUpload(command_id=command_id, result=result))


def get_command_bus(settings):
    """memory (in-process dev) or redis (prod cross-process Celery worker)."""
    if getattr(settings, "command_bus", "memory") == "redis":
        return RedisCommandBus(settings.celery_broker_url)
    return CommandBus()
