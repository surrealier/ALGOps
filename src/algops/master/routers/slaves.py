"""Runner ("slave") lifecycle: register, heartbeat, long-poll for commands, post results, upload.

All endpoints except ``register`` (enroll token) require the runner bearer token; the UI
listing (``ui_router``) is guarded by the API token like the rest of the API. Runners only ever make outbound calls — there is no inbound endpoint on a runner.
"""

from __future__ import annotations

import hmac

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from algops.core import (
    CommandResultUpload,
    HeartbeatRequest,
    LocalProjectDecl,
    NextCommandResponse,
    ProjectCreate,
    SlaveOut,
    SlaveRegisterRequest,
    SlaveRegisterResponse,
    manifest_from_wire,
)
from algops.tracking import get_session, repo

from ..services.projects import record_revision
from .deps import LONG_POLL_S, require_slave

router = APIRouter(prefix="/api/v1/slaves", tags=["slaves"])  # runner protocol (runner tokens)
ui_router = APIRouter(prefix="/api/v1/slaves", tags=["slaves"])  # UI reads (API token)


def _legacy_manifest(param_spec: dict | None, modality: str) -> dict:
    """A v1 slave registered a bare ParamSpec: describe it as a one-algorithm manifest."""
    spec = param_spec or {"params": []}
    return {
        "algops": 1,
        "legacy": True,
        "algorithms": [{
            "id": "default", "modality": modality, "params": None, "param_spec": spec,
            "entrypoint": {"python": "algops/run_adapter.py:run"},
            "metrics": {"presets": ["binary"]},
        }],
        "suites": [{"id": "default", "uri": ""}],
    }


@router.post("/register", response_model=SlaveRegisterResponse)
def register(body: SlaveRegisterRequest, request: Request) -> SlaveRegisterResponse:
    settings = request.app.state.settings
    if not hmac.compare_digest(body.enroll_token.encode(), settings.enroll_token.encode()):
        raise HTTPException(401, "invalid enroll token")
    decls = list(body.local_projects)
    v1 = False
    if body.project_name and not decls:
        v1 = True
        spec_dump = body.param_spec.model_dump(mode="json") if body.param_spec else None
        decls = [LocalProjectDecl(name=body.project_name, modality=body.modality, sha="local",
                                  manifest=_legacy_manifest(spec_dump, body.modality))]
    project_ids: dict[str, str] = {}
    to_record: list[tuple[str, LocalProjectDecl]] = []
    with get_session() as s:
        for d in decls:
            proj = repo.get_project_by_name(s, d.name)
            if proj is None:
                proj = repo.create_project(s, ProjectCreate(name=d.name, modality=d.modality), repo_kind="local")
            elif proj.repo_kind == "git":
                continue  # a git project owns this name; a runner cannot shadow it with a local path
            project_ids[d.name] = proj.project_id
            if d.manifest and d.sha:
                to_record.append((proj.project_id, d))
        first = next(iter(project_ids.values()), None)
        slave, token = repo.register_slave(
            s,
            project_id=first if v1 else None,
            host_label=body.host_label,
            labels=body.labels,
            concurrency=body.concurrency,
            allow_projects=body.allow_projects,
            git=body.git and not v1,
            local_projects=list(project_ids),
            version=body.version,
        )
        slave_id = slave.slave_id
    for pid, d in to_record:
        try:
            manifest_from_wire(d.manifest or {})
        except Exception:  # noqa: BLE001 — a broken manifest is reported by the next sync
            continue
        record_revision(pid, ref=None, sha=d.sha or "local", dirty=d.dirty, manifest=d.manifest or {})
    return SlaveRegisterResponse(slave_id=slave_id, project_id=first or "", slave_token=token,
                                 project_ids=project_ids)


@ui_router.get("", response_model=list[SlaveOut])
def list_slaves() -> list[SlaveOut]:
    with get_session() as s:
        return [
            SlaveOut(
                slave_id=sl.slave_id, host_label=sl.host_label, status=sl.status,
                online=repo.is_online(sl), labels=list(sl.labels or []), concurrency=sl.concurrency or 1,
                inflight=sl.inflight or 0, git=bool(sl.git), local_projects=list(sl.local_projects or []),
                allow_projects=sl.allow_projects, last_heartbeat=repo.iso(sl.last_heartbeat),
                version=sl.version or "",
            )
            for sl in repo.list_slaves(s)
        ]


@router.put("/{slave_id}/heartbeat")
def heartbeat(slave_id: str, body: HeartbeatRequest, _: str = Depends(require_slave)) -> dict:
    with get_session() as s:
        repo.touch_heartbeat(s, slave_id, body.status, inflight=body.inflight)
    return {"ok": True}


@router.get("/{slave_id}/next-command", response_model=NextCommandResponse)
async def next_command(slave_id: str, request: Request, _: str = Depends(require_slave)) -> NextCommandResponse:
    with get_session() as s:
        sl = repo.get_slave(s, slave_id)
        repo.touch_heartbeat(s, slave_id, sl.status if sl else "online")  # polling implies alive
    return await request.app.state.bus.next_command(slave_id, LONG_POLL_S)


@router.post("/{slave_id}/command-result")
async def command_result(slave_id: str, body: CommandResultUpload, request: Request,
                         _: str = Depends(require_slave)) -> dict:
    # Async so the future resolves on the event-loop thread (futures aren't thread-safe).
    delivered = await request.app.state.bus.deliver(body)
    return {"ok": delivered}


@router.post("/{slave_id}/artifacts")
async def upload_artifact(
    slave_id: str,
    request: Request,
    key: str = Form(...),
    file: UploadFile = File(...),
    _: str = Depends(require_slave),
) -> dict:
    if ".." in key.split("/") or key.startswith("/"):
        raise HTTPException(400, "invalid artifact key")
    data = await file.read()
    uri = request.app.state.store.put_bytes(key, data)
    return {"uri": uri}
