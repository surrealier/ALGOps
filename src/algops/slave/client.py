"""Thin async HTTP client the slave uses to talk to the master (outbound only)."""

from __future__ import annotations

import httpx

from algops.core import (
    HeartbeatRequest,
    NextCommandResponse,
    ParamSpec,
    RunResult,
    SlaveRegisterRequest,
    SlaveRegisterResponse,
    SlaveSettings,
)


class MasterClient:
    def __init__(self, settings: SlaveSettings) -> None:
        self.s = settings
        self.slave_id: str | None = None
        self.token: str | None = None
        self._client = httpx.AsyncClient(
            base_url=settings.master_url,
            timeout=httpx.Timeout(connect=10.0, read=settings.poll_timeout_s + 15.0, write=30.0, pool=10.0),
        )

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def register(self, param_spec: ParamSpec) -> SlaveRegisterResponse:
        body = SlaveRegisterRequest(
            enroll_token=self.s.enroll_token,
            project_name=self.s.project_name,
            modality=self.s.modality,
            host_label=self.s.host_label,
            param_spec=param_spec,
        )
        r = await self._client.post("/api/v1/slaves/register", json=body.model_dump())
        r.raise_for_status()
        resp = SlaveRegisterResponse(**r.json())
        self.slave_id = resp.slave_id
        self.token = resp.slave_token
        return resp

    async def heartbeat(self, status: str = "online") -> None:
        await self._client.put(
            f"/api/v1/slaves/{self.slave_id}/heartbeat",
            json=HeartbeatRequest(status=status).model_dump(),
            headers=self._auth(),
        )

    async def next_command(self) -> NextCommandResponse:
        r = await self._client.get(
            f"/api/v1/slaves/{self.slave_id}/next-command", headers=self._auth()
        )
        r.raise_for_status()
        return NextCommandResponse(**r.json())

    async def post_command_result(self, command_id: str, result: RunResult) -> bool:
        r = await self._client.post(
            f"/api/v1/slaves/{self.slave_id}/command-result",
            json={"command_id": command_id, "result": result.model_dump()},
            headers=self._auth(),
        )
        r.raise_for_status()
        return bool(r.json().get("ok"))

    async def upload_artifact(self, key: str, data: bytes, filename: str = "artifact.bin") -> str:
        r = await self._client.post(
            f"/api/v1/slaves/{self.slave_id}/artifacts",
            data={"key": key},
            files={"file": (filename, data, "application/octet-stream")},
            headers=self._auth(),
        )
        r.raise_for_status()
        return str(r.json()["uri"])

    async def aclose(self) -> None:
        await self._client.aclose()
