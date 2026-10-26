"""Thin async HTTP client the runner uses to talk to the master (outbound only)."""

from __future__ import annotations

import httpx

from algops.core import (
    CommandResultUpload,
    HeartbeatRequest,
    NextCommandResponse,
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
            timeout=httpx.Timeout(connect=10.0, read=settings.poll_timeout_s + 15.0, write=120.0, pool=10.0),
        )

    def _auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"} if self.token else {}

    async def register(self, body: SlaveRegisterRequest) -> SlaveRegisterResponse:
        r = await self._client.post("/api/v1/slaves/register", json=body.model_dump(mode="json"))
        r.raise_for_status()
        resp = SlaveRegisterResponse(**r.json())
        self.slave_id = resp.slave_id
        self.token = resp.slave_token
        return resp

    async def heartbeat(self, status: str = "online", inflight: int = 0) -> None:
        r = await self._client.put(
            f"/api/v1/slaves/{self.slave_id}/heartbeat",
            json=HeartbeatRequest(status=status, inflight=inflight).model_dump(),  # type: ignore[arg-type]
            headers=self._auth(),
        )
        r.raise_for_status()

    async def next_command(self) -> NextCommandResponse:
        r = await self._client.get(f"/api/v1/slaves/{self.slave_id}/next-command", headers=self._auth())
        r.raise_for_status()
        return NextCommandResponse(**r.json())

    async def post_upload(self, upload: CommandResultUpload) -> bool:
        r = await self._client.post(
            f"/api/v1/slaves/{self.slave_id}/command-result",
            json=upload.model_dump(mode="json"),
            headers=self._auth(),
        )
        r.raise_for_status()
        return bool(r.json().get("ok"))

    async def post_command_result(self, command_id: str, result) -> bool:  # v1 helper
        return await self.post_upload(CommandResultUpload(command_id=command_id, result=result))

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
