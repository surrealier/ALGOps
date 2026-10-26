"""Master FastAPI application factory (control plane + live WebSocket + UI + scheduler)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from algops import __version__
from algops.core import MasterSettings
from algops.tracking import init_engine

from .context import build_context
from .routers import artifacts, benchmarks, experiments, llm, projects, runs, schedules, slaves

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_FRONTEND_DIST = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "dist")
)


def _ui_dir() -> str | None:
    """A built React UI: ``frontend/dist`` (dev checkout / Docker) or the packaged copy."""
    for d in (_FRONTEND_DIST, os.path.join(_STATIC_DIR, "ui")):
        if os.path.isfile(os.path.join(d, "index.html")):
            return d
    return None


def create_app(settings: MasterSettings | None = None) -> FastAPI:
    settings = settings or MasterSettings()
    init_engine(settings)
    ctx = build_context(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        task = None
        if settings.scheduler_enabled:
            from .services.scheduler import Scheduler

            task = asyncio.create_task(Scheduler(ctx).run_forever())
        try:
            yield
        finally:
            if task is not None:
                task.cancel()

    app = FastAPI(title="ALGOps Master", version=__version__, lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    app.state.settings = settings
    app.state.ctx = ctx
    # Back-compat aliases (v1 code paths and tests read these directly).
    app.state.bus = ctx.bus
    app.state.ws = ctx.ws
    app.state.store = ctx.store
    app.state.rag = ctx.rag
    app.state.tasks = ctx.tasks

    for module in (projects, experiments, runs, slaves, artifacts, llm, benchmarks, schedules):
        app.include_router(module.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "dev_mode": settings.dev_mode, "version": __version__,
                "auth": bool(settings.api_token)}

    async def _ws(ws: WebSocket, channel: str) -> None:
        await ws.accept()
        await ctx.ws.subscribe(channel, ws)
        try:
            while True:
                await ws.receive_text()  # ignore inbound; keep the socket open
        except WebSocketDisconnect:
            pass
        finally:
            await ctx.ws.unsubscribe(channel, ws)

    @app.websocket("/ws/experiments/{experiment_id}")
    async def ws_experiment(ws: WebSocket, experiment_id: str) -> None:
        await _ws(ws, experiment_id)

    @app.websocket("/ws/benchmarks/{benchmark_id}")
    async def ws_benchmark(ws: WebSocket, benchmark_id: str) -> None:
        await _ws(ws, benchmark_id)

    ui = _ui_dir()
    if ui is not None:
        assets = os.path.join(ui, "assets")
        if os.path.isdir(assets):
            app.mount("/assets", StaticFiles(directory=assets), name="assets")

        @app.get("/", response_class=HTMLResponse)
        def index_built() -> FileResponse:
            return FileResponse(os.path.join(ui, "index.html"))
    else:

        @app.get("/", response_class=HTMLResponse)
        def index_bundled() -> FileResponse:
            return FileResponse(os.path.join(_STATIC_DIR, "index.html"))

    return app
