"""MasterContext — the shared services every master-side workflow needs.

Built once by ``create_app`` (API process) and once per Celery task (worker process) so the
benchmark / campaign / scheduler services do not depend on FastAPI request objects.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from typing import Any

from algops.core import MasterSettings

from .dispatch import Dispatcher


@dataclass
class MasterContext:
    settings: MasterSettings
    bus: Any
    ws: Any
    store: Any
    rag: Any = None
    dispatcher: Dispatcher | None = None
    tasks: set = field(default_factory=set)

    def __post_init__(self) -> None:
        if self.dispatcher is None:
            self.dispatcher = Dispatcher(self.bus, self.settings)

    def spawn(self, coro: Coroutine) -> asyncio.Task:
        """Run a workflow in the background, keeping a strong reference until it finishes."""
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task


def build_context(settings: MasterSettings, *, bus=None, ws=None, store=None, rag=None) -> MasterContext:
    from algops.rag import get_rag_store
    from algops.tracking import get_artifact_store

    from .bus import get_command_bus
    from .ws import get_ws_broker

    return MasterContext(
        settings=settings,
        bus=bus if bus is not None else get_command_bus(settings),
        ws=ws if ws is not None else get_ws_broker(settings),
        store=store if store is not None else get_artifact_store(settings),
        rag=rag if rag is not None else get_rag_store(settings),
    )
