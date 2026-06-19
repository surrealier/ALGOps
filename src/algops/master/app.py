"""Master FastAPI application factory (control plane + live WebSocket + UI)."""

from __future__ import annotations

import os

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from algops.core import MasterSettings
from algops.rag import get_rag_store
from algops.tracking import get_artifact_store, init_engine

from .bus import get_command_bus
from .routers import artifacts, experiments, llm, projects, runs, slaves
from .ws import get_ws_broker

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_FRONTEND_DIST = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "frontend", "dist")
)


def create_app(settings: MasterSettings | None = None) -> FastAPI:
    settings = settings or MasterSettings()
    init_engine(settings)

    app = FastAPI(title="ALGOps Master", version="0.0.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.state.settings = settings
    app.state.bus = get_command_bus(settings)
    app.state.ws = get_ws_broker(settings)
    app.state.store = get_artifact_store(settings)
    app.state.rag = get_rag_store(settings)  # in-memory (dev) or Qdrant+Neo4j (prod)
    app.state.tasks = set()

    for module in (projects, experiments, runs, slaves, artifacts, llm):
        app.include_router(module.router)

    @app.get("/healthz")
    def healthz() -> dict:
        return {"status": "ok", "dev_mode": settings.dev_mode}

    @app.websocket("/ws/experiments/{experiment_id}")
    async def ws_experiment(ws: WebSocket, experiment_id: str) -> None:
        await ws.accept()
        await app.state.ws.subscribe(experiment_id, ws)
        try:
            while True:
                await ws.receive_text()  # ignore inbound; keep the socket open
        except WebSocketDisconnect:
            pass
        finally:
            await app.state.ws.unsubscribe(experiment_id, ws)

    # Prefer a built React app if present; otherwise serve the bundled single-file UI.
    if os.path.isdir(_FRONTEND_DIST):
        app.mount("/assets", StaticFiles(directory=os.path.join(_FRONTEND_DIST, "assets")), name="assets")

        @app.get("/", response_class=HTMLResponse)
        def index_built() -> FileResponse:
            return FileResponse(os.path.join(_FRONTEND_DIST, "index.html"))
    else:

        @app.get("/", response_class=HTMLResponse)
        def index_bundled() -> FileResponse:
            return FileResponse(os.path.join(_STATIC_DIR, "index.html"))

    return app
