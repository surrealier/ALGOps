"""Dispatcher — choose a runner for each command, watch it, and re-dispatch on infra failure.

Placement rule (``repo.can_serve``): local projects only go to the runner(s) that declared them;
git projects go to any git-capable runner whose allowlist admits the project and whose labels
cover the algorithm's ``runs_on``. Among eligible runners the least loaded (in-flight /
concurrency) wins. While a command is in flight the dispatcher keeps checking the runner's
heartbeat; if it goes silent the command is withdrawn and re-dispatched (``run_retries``).
"""

from __future__ import annotations

import asyncio
import random
import time

from algops.core import (
    CommandKind,
    MasterSettings,
    NextCommandResponse,
    PromoteRequest,
    PromoteResult,
    RepoRef,
    RunRequest,
    RunResult,
    RunStatus,
    SyncRequest,
    SyncResult,
)
from algops.tracking import get_session, repo
from algops.tracking.models import Project

WAIT_SLICE_S = 5.0


class NoRunnerError(RuntimeError):
    pass


def repo_ref(project: Project, *, ref: str | None = None, sha: str | None = None) -> RepoRef:
    return RepoRef(
        kind="git" if project.repo_kind == "git" else "local",
        name=project.name,
        url=project.repo_url,
        ref=ref if ref is not None else project.default_ref,
        sha=sha,
    )


class Dispatcher:
    def __init__(self, bus, settings: MasterSettings, *, wait_for_runner_s: float = 30.0) -> None:
        self.bus = bus
        self.settings = settings
        self.wait_for_runner_s = wait_for_runner_s
        self._assigned: dict[str, int] = {}

    # ------------------------------------------------------------------ placement

    def candidates(self, project_id: str, runs_on: list[str] | None = None,
                   exclude: set[str] | None = None) -> list[tuple[str, float]]:
        with get_session() as s:
            proj = repo.get_project(s, project_id)
            if proj is None:
                return []
            out = []
            for sl in repo.eligible_slaves(s, proj, runs_on):
                if exclude and sl.slave_id in exclude:
                    continue
                load = max(self._assigned.get(sl.slave_id, 0), int(sl.inflight or 0))
                out.append((sl.slave_id, load / max(1, sl.concurrency or 1)))
        random.shuffle(out)
        out.sort(key=lambda x: x[1])
        return out

    async def choose(self, project_id: str, runs_on: list[str] | None = None,
                     exclude: set[str] | None = None) -> str:
        deadline = time.monotonic() + self.wait_for_runner_s
        while True:
            cands = self.candidates(project_id, runs_on, exclude)
            if cands:
                return cands[0][0]
            if time.monotonic() >= deadline:
                need = f" with labels {runs_on}" if runs_on else ""
                raise NoRunnerError(f"no online runner can serve this project{need}")
            await asyncio.sleep(1.0)

    def capacity(self, project_ids: list[str] | None = None) -> int:
        with get_session() as s:
            slaves = repo.online_slaves(s)
            if project_ids:
                projs = [p for p in (repo.get_project(s, pid) for pid in project_ids) if p]
                slaves = [sl for sl in slaves if any(repo.can_serve(sl, p) for p in projs)]
            return sum(max(1, sl.concurrency or 1) for sl in slaves)

    @staticmethod
    def _alive(slave_id: str) -> bool:
        with get_session() as s:
            sl = repo.get_slave(s, slave_id)
            return sl is not None and repo.is_online(sl)

    # ------------------------------------------------------------------ core

    async def _send(self, slave_id: str, cmd: NextCommandResponse, timeout: float):
        """Enqueue + wait in slices; returns the upload, or None if the runner died / timed out."""
        self._assigned[slave_id] = self._assigned.get(slave_id, 0) + 1
        cid = await self.bus.enqueue(slave_id, cmd)
        deadline = time.monotonic() + timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None, "timeout"
                up = await self.bus.wait(cid, min(WAIT_SLICE_S, remaining))
                if up is not None:
                    return up, None
                if not self._alive(slave_id):
                    return None, "runner went offline"
        finally:
            self._assigned[slave_id] = max(0, self._assigned.get(slave_id, 1) - 1)
            await self.bus.forget(cid)

    async def run(self, req: RunRequest, *, runs_on: list[str] | None = None,
                  timeout: float | None = None) -> tuple[RunResult, str | None]:
        """Execute a run somewhere eligible. Never raises: failures come back as a RunResult."""
        timeout = timeout or self.settings.command_timeout_s
        tried: set[str] = set()
        last: RunResult | None = None
        for _ in range(1 + max(0, self.settings.run_retries)):
            try:
                slave_id = await self.choose(req.project_id, runs_on, exclude=tried)
            except NoRunnerError as exc:
                if last is not None:
                    return last, None
                return RunResult(run_id=req.run_id, status=RunStatus.failed, error=str(exc),
                                 error_kind="infra"), None
            cmd = NextCommandResponse(kind=CommandKind.run, run_spec=req)
            up, why = await self._send(slave_id, cmd, timeout)
            tried.add(slave_id)
            if up is None or up.result is None:
                kind = "timeout" if why == "timeout" else "infra"
                last = RunResult(run_id=req.run_id, status=RunStatus.failed, error_kind=kind,
                                 error=f"runner {slave_id[:8]}: {why or 'no result'}")
                continue
            res = up.result
            if res.status == RunStatus.failed and res.error_kind == "infra":
                last = res
                continue
            return res, slave_id
        assert last is not None
        return last, None

    async def sync(self, project_id: str, ref: str | None = None) -> SyncResult:
        with get_session() as s:
            proj = repo.get_project(s, project_id)
            if proj is None:
                return SyncResult(ok=False, error="project not found")
            rref = repo_ref(proj, ref=ref)
        tried: set[str] = set()
        last = SyncResult(ok=False, error="no attempt")
        for _ in range(2):
            try:
                slave_id = await self.choose(project_id, exclude=tried)
            except NoRunnerError as exc:
                return last if tried else SyncResult(ok=False, error=str(exc))
            cmd = NextCommandResponse(kind=CommandKind.sync,
                                      sync_spec=SyncRequest(project_id=project_id, repo=rref))
            up, why = await self._send(slave_id, cmd, self.settings.sync_timeout_s)
            tried.add(slave_id)
            if up is not None and up.sync_result is not None:
                return up.sync_result
            last = SyncResult(ok=False, error=f"runner {slave_id[:8]}: {why or 'no sync result'}")
        return last

    async def promote(self, req: PromoteRequest) -> PromoteResult:
        try:
            slave_id = await self.choose(req.project_id)
        except NoRunnerError as exc:
            return PromoteResult(ok=False, error=str(exc))
        cmd = NextCommandResponse(kind=CommandKind.promote, promote_spec=req)
        up, why = await self._send(slave_id, cmd, self.settings.sync_timeout_s)
        if up is None or up.promote_result is None:
            return PromoteResult(ok=False, error=f"runner {slave_id[:8]}: {why or 'no result'}")
        return up.promote_result
