"""Slave lifecycle: register, heartbeat, long-poll for commands, post results, upload artifacts.

All endpoints except ``register`` require the project-scoped slave bearer token. The slave only
ever makes outbound calls — there is no inbound endpoint on the slave (NAT/firewall friendly).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile

from algops.core import (
    CommandResultUpload,
    HeartbeatRequest,
    NextCommandResponse,
    ProjectCreate,
    SlaveRegisterRequest,
    SlaveRegisterResponse,
)
from algops.tracking import get_session, repo

from .deps import LONG_POLL_S, require_slave

router = APIRouter(prefix="/api/v1/slaves", tags=["slaves"])


@router.post("/register", response_model=SlaveRegisterResponse)
def register(body: SlaveRegisterRequest, request: Request) -> SlaveRegisterResponse:
    settings = request.app.state.settings
    if body.enroll_token != settings.enroll_token:
        raise HTTPException(401, "invalid enroll token")
    spec_dump = body.param_spec.model_dump() if body.param_spec else None
    with get_session() as s:
        proj = next((p for p in repo.list_projects(s) if p.name == body.project_name), None)
        if proj is None:
            proj = repo.create_project(
                s, ProjectCreate(name=body.project_name, modality=body.modality), param_spec=spec_dump
            )
        elif spec_dump is not None:
            proj.param_spec = spec_dump
        slave, token = repo.register_slave(s, proj.project_id, body.host_label, param_spec=spec_dump)
        return SlaveRegisterResponse(
            slave_id=slave.slave_id, project_id=proj.project_id, slave_token=token
        )


@router.put("/{slave_id}/heartbeat")
def heartbeat(slave_id: str, body: HeartbeatRequest, _: str = Depends(require_slave)) -> dict:
    with get_session() as s:
        repo.touch_heartbeat(s, slave_id, body.status)
    return {"ok": True}


@router.get("/{slave_id}/next-command", response_model=NextCommandResponse)
async def next_command(
    slave_id: str, request: Request, _: str = Depends(require_slave)
) -> NextCommandResponse:
    with get_session() as s:
        repo.touch_heartbeat(s, slave_id)  # polling implies the slave is alive
    return await request.app.state.bus.next_command(slave_id, LONG_POLL_S)


@router.post("/{slave_id}/command-result")
async def command_result(
    slave_id: str, body: CommandResultUpload, request: Request, _: str = Depends(require_slave)
) -> dict:
    # Async so set_result runs on the event-loop thread (futures aren't thread-safe).
    delivered = await request.app.state.bus.deliver_result(body.command_id, body.result)
    return {"ok": delivered}


@router.post("/{slave_id}/artifacts")
async def upload_artifact(
    slave_id: str,
    request: Request,
    key: str = Form(...),
    file: UploadFile = File(...),
    _: str = Depends(require_slave),
) -> dict:
    data = await file.read()
    uri = request.app.state.store.put_bytes(key, data)
    return {"uri": uri}
