"""Celery app + workflow tasks (production task runtime).

The cross-process path requires ``command_bus='redis'`` + ``ws_backend='redis'`` so the worker
(here) and the API process share the command bus and progress channel via Redis. Durability of
campaigns is the LangGraph Postgres checkpoint (thread = experiment attempt) — a worker restart
resumes. For single-host dev keep ``dev_mode=true`` (workflows run in the API process).
"""

from __future__ import annotations

import asyncio

from celery import Celery

from algops.core import MasterSettings

_settings = MasterSettings()
celery = Celery("algops", broker=_settings.celery_broker_url, backend=_settings.celery_broker_url)


def _ctx():  # pragma: no cover - requires Redis/Celery
    from algops.tracking import init_engine

    from .context import build_context

    settings = MasterSettings()
    init_engine(settings)
    return build_context(settings)


@celery.task(name="algops.run_experiment")
def run_experiment_task(experiment_id: str) -> str:  # pragma: no cover
    from .loop import run_experiment_loop

    asyncio.run(run_experiment_loop(experiment_id=experiment_id, ctx=_ctx()))
    return "ok"


@celery.task(name="algops.resume_experiment")
def resume_experiment_task(experiment_id: str, approved: bool) -> str:  # pragma: no cover
    from .loop import resume_experiment_loop

    asyncio.run(resume_experiment_loop(experiment_id=experiment_id, approved=bool(approved), ctx=_ctx()))
    return "ok"


@celery.task(name="algops.run_benchmark")
def run_benchmark_task(benchmark_id: str) -> str:  # pragma: no cover
    from .services.benchmarks import run_benchmark

    return asyncio.run(run_benchmark(_ctx(), benchmark_id))
