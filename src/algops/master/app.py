"""Master FastAPI application factory (control plane + live WebSocket + UI + scheduler)."""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from algops import __version__
from algops.core import MasterSettings
from algops.core.config import DEFAULT_ENROLL_TOKEN
from algops.tracking import init_engine

from .context import build_context
from .routers import artifacts, benchmarks, experiments, llm, projects, runs, schedules, slaves
from .routers.deps import api_token_ok, require_api

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")


def _ui_dir() -> str | None:
    """The React UI built into the package (``cd frontend && npm run build``)."""
    d = os.environ.get("ALGOPS_MASTER_UI_DIR") or os.path.join(_STATIC_DIR, "ui")
    return d if os.path.isfile(os.path.join(d, "index.html")) else None


def create_app(settings: MasterSettings | None = None) -> FastAPI:
    settings = settings or MasterSettings()
    if not settings.dev_mode and settings.enroll_token == DEFAULT_ENROLL_TOKEN:
        raise RuntimeError("refusing to start: set ALGOPS_MASTER_ENROLL_TOKEN (the default dev enroll "
                           "token lets anyone register a runner) or run with dev_mode=true")
    init_engine(settings)
    if settings.dev_mode:  # workflows run in-process: whatever "running" row exists is orphaned
        from algops.tracking import get_session, repo

        with get_session() as s:
            fixed = repo.reconcile_orphans(s)
        if any(fixed.values()):
            print(f"[master] 재시작 정리: 중단된 캠페인 {fixed['experiments']}개, 벤치마크 "
                  f"{fixed['benchmarks']}개를 실패 처리했습니다 (다시 실행하면 새 attempt).", flush=True)
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
    origins = settings.cors_origins if settings.cors_origins is not None else (["*"] if settings.dev_mode else [])
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["*"], allow_headers=["*"])

    app.state.settings = settings
    app.state.ctx = ctx
    # Back-compat aliases (v1 code paths and tests read these directly).
    app.state.bus = ctx.bus
    app.state.ws = ctx.ws
    app.state.store = ctx.store
    app.state.rag = ctx.rag
    app.state.tasks = ctx.tasks

    # Runner protocol endpoints authenticate with runner tokens; everything else with the API token.
    app.include_router(slaves.router)
    app.include_router(slaves.ui_router, dependencies=[Depends(require_api)])
    for module in (projects, experiments, runs, artifacts, llm, benchmarks, schedules):
        app.include_router(module.router, dependencies=[Depends(require_api)])

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "dev_mode": settings.dev_mode, "version": __version__,
                "auth": bool(settings.api_token)}

    async def _ws(ws: WebSocket, channel: str) -> None:
        if not api_token_ok(settings.api_token, ws.query_params.get("token")):
            await ws.close(code=1008)  # policy violation: API token required
            return
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
