"""The runner ("slave") process: register, heartbeat, and long-poll the master for commands.

Outbound-only (works behind NAT/firewalls). ``concurrency`` workers long-poll in parallel, so a
host executes that many runs at once; a separate heartbeat keeps the runner visible while every
worker is busy with long runs (the master re-dispatches if heartbeats stop).

Commands: ``run`` (execute + upload artifacts), ``sync`` (resolve ref, read manifest),
``promote`` (branch commit of proposed parameter defaults).
"""

from __future__ import annotations

import asyncio
import os
import shutil
import socket

import httpx

from algops import __version__
from algops.core import (
    CommandKind,
    CommandResultUpload,
    NextCommandResponse,
    PromoteResult,
    RunResult,
    RunStatus,
    SlaveRegisterRequest,
    SlaveSettings,
    SyncResult,
)
from algops.runner import RunnerEngine, bundled_toy_path
from algops.runner.process import DEFAULT_WITHHELD_ENV, EnvPolicy
from algops.runner.protocol import RunOutcome

from .client import MasterClient


def local_project_map(settings: SlaveSettings) -> dict[str, str]:
    projects = dict(settings.local_projects)
    if settings.project_path:
        projects[settings.project_name] = settings.project_path
    if not projects and settings.serve_toy:
        projects[settings.project_name or "toy-project"] = bundled_toy_path()
    return projects


def env_policy(settings: SlaveSettings) -> EnvPolicy:
    withhold = tuple(settings.withhold_env) if settings.withhold_env is not None else DEFAULT_WITHHELD_ENV
    return EnvPolicy(withhold=withhold, pass_env=tuple(settings.pass_env))


def build_engine(settings: SlaveSettings) -> RunnerEngine:
    return RunnerEngine(
        settings.work_dir,
        local_projects=local_project_map(settings),
        keep_checkouts=settings.keep_checkouts,
        git_enabled=settings.git,
        author_name=settings.git_author_name,
        author_email=settings.git_author_email,
        push_remote=settings.push_remote,
        env_policy=env_policy(settings),
    )


class Runner:
    def __init__(self, settings: SlaveSettings) -> None:
        self.s = settings
        self.engine = build_engine(settings)
        self.client = MasterClient(settings)
        self.inflight = 0
        self._reg_lock = asyncio.Lock()
        self._generation = 0  # bumps on every (re-)registration

    async def _register_body(self) -> SlaveRegisterRequest:
        return SlaveRegisterRequest(
            enroll_token=self.s.enroll_token,
            host_label=self.s.host_label if self.s.host_label != "dev-pc" else (socket.gethostname() or "dev-pc"),
            labels=self.s.labels,
            concurrency=self.s.concurrency,
            allow_projects=self.s.allow_projects,
            git=self.s.git,
            local_projects=await self.engine.local_decls(),
            version=__version__,
        )

    async def register(self, generation: int | None = None) -> None:
        async with self._reg_lock:
            if generation is not None and generation != self._generation:
                return  # another worker already re-registered
            while True:
                try:
                    resp = await self.client.register(await self._register_body())
                    self._generation += 1
                    names = ", ".join(resp.project_ids) or "-"
                    print(f"[runner] 등록 완료 — slave_id={resp.slave_id[:8]} "
                          f"concurrency={self.s.concurrency} labels={self.s.labels or '-'} local={names}",
                          flush=True)
                    return
                except Exception as exc:  # noqa: BLE001 — master not up yet / transient
                    print(f"[runner] 마스터 등록 대기 ({self.s.master_url}): {exc}", flush=True)
                    await asyncio.sleep(3)

    # ------------------------------------------------------------------ loops

    async def heartbeat_loop(self) -> None:
        while True:
            await asyncio.sleep(self.s.heartbeat_interval_s)
            gen = self._generation
            try:
                await self.client.heartbeat("busy" if self.inflight else "online", self.inflight)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 404):
                    await self.register(gen)
            except Exception:  # noqa: BLE001 — connection blip; the next beat retries
                pass

    async def worker(self, idx: int) -> None:
        while True:
            gen = self._generation
            try:
                cmd = await self.client.next_command()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (401, 404):
                    print("[runner] 토큰 만료/무효 → 재등록", flush=True)
                    await self.register(gen)
                else:
                    await asyncio.sleep(2)
                continue
            except Exception as exc:  # noqa: BLE001 — connection blip
                print(f"[runner] poll 오류: {exc}", flush=True)
                await asyncio.sleep(2)
                continue
            if cmd.kind == CommandKind.noop or cmd.command_id is None:
                continue
            self.inflight += 1
            try:
                upload = await self.handle(cmd)
            finally:
                self.inflight -= 1
            for attempt in range(5):
                try:
                    await self.client.post_upload(upload)
                    break
                except Exception as exc:  # noqa: BLE001
                    print(f"[runner] 결과 전송 재시도 {attempt + 1}/5: {exc}", flush=True)
                    await asyncio.sleep(2 * (attempt + 1))

    # ------------------------------------------------------------------ commands

    async def handle(self, cmd: NextCommandResponse) -> CommandResultUpload:
        cid = cmd.command_id or ""
        try:
            if cmd.kind == CommandKind.run and cmd.run_spec is not None:
                req = cmd.run_spec
                where = f"{req.repo.name if req.repo else '?'}:{req.algorithm_id}@{req.suite_id}"
                print(f"[runner] run {req.run_id[:8]} {where} params={req.param_values}", flush=True)
                outcome = await self.engine.run(req)
                try:
                    result = await self._upload_outcome(outcome)
                finally:
                    if outcome.run_dir:  # artifacts now live on the master; keep the runner disk bounded
                        shutil.rmtree(outcome.run_dir, ignore_errors=True)
                print(f"[runner]   → {result.status.value} ({result.duration_s or 0:.2f}s)"
                      + (f" [{result.error_kind}] {str(result.error)[:200]}" if result.error else ""), flush=True)
                return CommandResultUpload(command_id=cid, result=result)
            if cmd.kind == CommandKind.sync and cmd.sync_spec is not None:
                res = await self.engine.sync(cmd.sync_spec)
                print(f"[runner] sync {cmd.sync_spec.repo.name} → {res.sha or res.error}", flush=True)
                return CommandResultUpload(command_id=cid, sync_result=res)
            if cmd.kind == CommandKind.promote and cmd.promote_spec is not None:
                res = await self.engine.promote(cmd.promote_spec)
                print(f"[runner] promote {cmd.promote_spec.branch} → {res.commit_sha or res.error}", flush=True)
                return CommandResultUpload(command_id=cid, promote_result=res)
        except Exception as exc:  # noqa: BLE001 — never lose a command silently
            msg = f"{type(exc).__name__}: {exc}"
            if cmd.run_spec is not None:
                return CommandResultUpload(command_id=cid, result=RunResult(
                    run_id=cmd.run_spec.run_id, status=RunStatus.failed, error=msg, error_kind="infra"))
            if cmd.sync_spec is not None:
                return CommandResultUpload(command_id=cid, sync_result=SyncResult(ok=False, error=msg))
            return CommandResultUpload(command_id=cid, promote_result=PromoteResult(ok=False, error=msg))
        return CommandResultUpload(command_id=cid)

    async def _upload_outcome(self, outcome: RunOutcome) -> RunResult:
        """Upload every artifact through the master (proxied) and repoint uris at the uploads."""
        result = outcome.result
        if not outcome.files:
            return result
        out_dir = os.path.join(outcome.run_dir, "out") if outcome.run_dir else None
        telemetry_rel = None
        if result.csv_manifest_uri and out_dir:
            telemetry_rel = os.path.relpath(result.csv_manifest_uri, out_dir).replace(os.sep, "/")
        updates: dict = {}
        failed: list[str] = []
        for name, data in outcome.files.items():
            try:
                uri = await self.client.upload_artifact(f"runs/{result.run_id}/{name}", data,
                                                        os.path.basename(name))
            except Exception:  # noqa: BLE001
                failed.append(name)
                continue
            if name == telemetry_rel:
                updates["csv_manifest_uri"] = uri
            elif name == "_algops/log.txt":
                updates["log_uri"] = uri
        if telemetry_rel and "csv_manifest_uri" not in updates:
            updates["csv_manifest_uri"] = None  # a runner-local path is useless to the master
        if failed:
            extra = dict(result.extra)
            extra["artifact_upload_failed"] = failed[:20]
            updates["extra"] = extra
        return result.model_copy(update=updates)


async def run_slave(settings: SlaveSettings | None = None) -> None:
    settings = settings or SlaveSettings()
    os.makedirs(settings.work_dir, exist_ok=True)
    runner = Runner(settings)
    withheld = runner.engine.env_policy.withheld()
    if withheld:
        print(f"[runner] 저장소 코드에 전달하지 않는 환경변수 {len(withheld)}개: {', '.join(withheld[:12])}"
              f"{' …' if len(withheld) > 12 else ''} (필요하면 ALGOPS_SLAVE_PASS_ENV로 허용)", flush=True)
    await runner.register()
    tasks = [asyncio.create_task(runner.heartbeat_loop())]
    tasks += [asyncio.create_task(runner.worker(i)) for i in range(max(1, settings.concurrency))]
    try:
        await asyncio.gather(*tasks)
    finally:
        for t in tasks:
            t.cancel()
        await runner.client.aclose()
