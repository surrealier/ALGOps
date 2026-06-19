"""The slave worker loop: register, then long-poll the master for run commands.

The slave makes only outbound calls, so it works behind NAT/firewalls on a developer PC. The
master's next-command long-poll doubles as the heartbeat (it touches last_heartbeat server-side).
"""

from __future__ import annotations

import asyncio
import os

import httpx

from algops.core import CommandKind, RunResult, RunStatus, SlaveSettings

from .client import MasterClient
from .runner import execute_run, resolve_project


async def _register_with_retry(client: MasterClient, spec) -> None:
    while True:
        try:
            resp = await client.register(spec)
            print(
                f"[slave] 등록 완료 — slave_id={resp.slave_id[:8]} project_id={resp.project_id[:8]} "
                f"project='{client.s.project_name}'",
                flush=True,
            )
            return
        except Exception as exc:  # master not up yet / transient
            print(f"[slave] 마스터 등록 대기 ({client.s.master_url}): {exc}", flush=True)
            await asyncio.sleep(3)


async def run_slave(settings: SlaveSettings | None = None) -> None:
    settings = settings or SlaveSettings()
    run_fn, spec = resolve_project(settings.project_path)
    os.makedirs(settings.work_dir, exist_ok=True)
    client = MasterClient(settings)
    await _register_with_retry(client, spec)

    try:
        while True:
            try:
                cmd = await client.next_command()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 401:
                    print("[slave] 토큰 만료/무효 → 재등록", flush=True)
                    await _register_with_retry(client, spec)
                else:
                    await asyncio.sleep(2)
                continue
            except Exception as exc:  # connection blip
                print(f"[slave] poll 오류: {exc}", flush=True)
                await asyncio.sleep(2)
                continue

            if cmd.kind != CommandKind.run or cmd.run_spec is None:
                continue

            req = cmd.run_spec
            print(f"[slave] run {req.run_id[:8]} params={req.param_values}", flush=True)
            try:
                result, files = await asyncio.to_thread(execute_run, run_fn, req, settings.work_dir)
                # Upload every stage CSV + manifest through the master (proxied artifacts),
                # then repoint csv_manifest_uri at the uploaded manifest.
                manifest_uri = None
                for name, data in files.items():
                    uri = await client.upload_artifact(f"runs/{req.run_id}/{name}", data, name)
                    if name == "manifest.json":
                        manifest_uri = uri
                if manifest_uri:
                    result = result.model_copy(update={"csv_manifest_uri": manifest_uri})
            except Exception as exc:  # noqa: BLE001
                result = RunResult(run_id=req.run_id, status=RunStatus.failed, error=str(exc))
            await client.post_command_result(cmd.command_id, result)
    finally:
        await client.aclose()
