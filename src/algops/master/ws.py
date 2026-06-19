"""Live progress fan-out to the browser over WebSocket (per experiment).

In-process broker for dev; Redis pub/sub broker for prod (the Celery worker publishes events,
the API process subscribes and forwards them to connected browsers).
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from fastapi import WebSocket


class WsBroker:
    def __init__(self) -> None:
        self._subs: dict[str, set[WebSocket]] = {}
        self._lock = asyncio.Lock()

    async def subscribe(self, experiment_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._subs.setdefault(experiment_id, set()).add(ws)

    async def unsubscribe(self, experiment_id: str, ws: WebSocket) -> None:
        async with self._lock:
            self._subs.get(experiment_id, set()).discard(ws)

    async def publish(self, experiment_id: str, message: dict[str, Any]) -> None:
        for ws in list(self._subs.get(experiment_id, set())):
            try:
                await ws.send_json(message)
            except Exception:
                await self.unsubscribe(experiment_id, ws)


class RedisWsBroker:  # pragma: no cover - requires Redis (prod cross-process progress)
    def __init__(self, url: str) -> None:
        import redis.asyncio as redis

        self._r = redis.from_url(url)
        self._tasks: dict[int, asyncio.Task] = {}

    async def subscribe(self, experiment_id: str, ws: WebSocket) -> None:
        pubsub = self._r.pubsub()
        await pubsub.subscribe(f"algops:ws:{experiment_id}")

        async def _forward() -> None:
            try:
                async for msg in pubsub.listen():
                    if msg.get("type") == "message":
                        data = msg["data"]
                        await ws.send_text(data.decode() if isinstance(data, bytes) else data)
            except Exception:
                pass

        self._tasks[id(ws)] = asyncio.create_task(_forward())

    async def unsubscribe(self, experiment_id: str, ws: WebSocket) -> None:
        task = self._tasks.pop(id(ws), None)
        if task is not None:
            task.cancel()

    async def publish(self, experiment_id: str, message: dict[str, Any]) -> None:
        await self._r.publish(f"algops:ws:{experiment_id}", json.dumps(message, default=str))


def get_ws_broker(settings):
    if getattr(settings, "ws_backend", "memory") == "redis":
        return RedisWsBroker(settings.celery_broker_url)
    return WsBroker()
