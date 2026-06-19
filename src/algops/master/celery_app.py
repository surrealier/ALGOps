"""Celery app + experiment-loop tasks (production task runtime, M5).

The cross-process path requires ``command_bus='redis'`` + ``ws_backend='redis'`` so the worker
(here) and the API process share the bus and progress channel via Redis. Durability is the
LangGraph Postgres checkpoint (thread_id == experiment_id) — a worker restart resumes the
campaign. For single-host dev, keep ``dev_mode=true`` (the loop runs in the API process).
"""

from __future__ import annotations

import asyncio

from celery import Celery

from algops.core import MasterSettings

_settings = MasterSettings()
celery = Celery("algops", broker=_settings.celery_broker_url, backend=_settings.celery_broker_url)


def _run(experiment_id: str, resume_value=None) -> str:  # pragma: no cover - requires Redis/Celery
    from algops.rag import get_rag_store
    from algops.tracking import get_artifact_store, init_engine

    from .bus import RedisCommandBus
    from .loop import run_experiment_loop
    from .ws import RedisWsBroker

    settings = MasterSettings()
    init_engine(settings)
    asyncio.run(
        run_experiment_loop(
            experiment_id=experiment_id,
            bus=RedisCommandBus(settings.celery_broker_url),
            ws_broker=RedisWsBroker(settings.celery_broker_url),
            artifact_store=get_artifact_store(settings),
            settings=settings,
            rag_store=get_rag_store(settings),
            resume_value=resume_value,
        )
    )
    return "ok"


@celery.task(name="algops.run_experiment")
def run_experiment_task(experiment_id: str) -> str:  # pragma: no cover
    return _run(experiment_id)


@celery.task(name="algops.resume_experiment")
def resume_experiment_task(experiment_id: str, approved: bool) -> str:  # pragma: no cover
    return _run(experiment_id, resume_value=bool(approved))
