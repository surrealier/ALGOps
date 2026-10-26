"""Dispatcher placement + re-dispatch, scheduler cron ticks, PR/MR creation."""

from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from algops.core import (
    CommandKind,
    CommandResultUpload,
    MasterSettings,
    ProjectCreate,
    RepoRef,
    RunRequest,
    RunResult,
    RunStatus,
    new_id,
)
from algops.master.bus import CommandBus
from algops.master.context import MasterContext
from algops.master.services.promotion import open_pull_request, repo_path
from algops.master.services.scheduler import Scheduler
from algops.master.ws import WsBroker
from algops.tracking import get_session, init_engine, repo
from algops.tracking.artifacts import LocalArtifactStore
from algops.tracking.db import reset_engine


@pytest.fixture
def db(tmp_path):
    reset_engine()
    settings = MasterSettings(db_url=f"sqlite+pysqlite:///{tmp_path / 't.sqlite'}",
                              artifact_dir=str(tmp_path / "a"), run_retries=1, scheduler_enabled=False)
    init_engine(settings)
    yield settings
    reset_engine()


def _slave(s, **kw):
    slave, _ = repo.register_slave(s, host_label=kw.pop("host", "h"), **kw)
    return slave.slave_id


def test_placement_rules(db):
    with get_session() as s:
        git_p = repo.create_project(s, ProjectCreate(name="g", repo_url="file:///x"))
        loc_p = repo.create_project(s, ProjectCreate(name="loc"), repo_kind="local")
        a = _slave(s, labels=["cpu"])
        b = _slave(s, labels=["gpu", "cuda"])
        c = _slave(s, git=False, local_projects=["loc"])
        d = _slave(s, labels=["gpu"], allow_projects=["other"])
        ids = lambda xs: {x.slave_id for x in xs}  # noqa: E731
        assert ids(repo.eligible_slaves(s, git_p)) == {a, b, d} - {d}  # d's allowlist excludes g
        assert ids(repo.eligible_slaves(s, git_p, ["gpu"])) == {b}
        assert ids(repo.eligible_slaves(s, loc_p)) == {c}
        stale = repo.get_slave(s, a)
        stale.last_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=5)
        assert a not in ids(repo.eligible_slaves(s, git_p))


async def test_dispatcher_redispatches_when_a_runner_dies(db):
    from algops.master import dispatch as dispatch_mod

    with get_session() as s:
        proj = repo.create_project(s, ProjectCreate(name="g", repo_url="file:///x"))
        dead = _slave(s, host="dead")
        alive = _slave(s, host="alive")
        pid = proj.project_id
    bus = CommandBus()
    ctx = MasterContext(settings=db, bus=bus, ws=WsBroker(), store=None)
    dispatch_mod.WAIT_SLICE_S = 0.2
    assert ctx.dispatcher is not None
    # Make the dispatcher pick the dead runner first, then kill its heartbeat.
    ctx.dispatcher.candidates = (lambda orig: lambda *a, **k: sorted(orig(*a, **k), key=lambda x: x[0] != dead))(
        ctx.dispatcher.candidates)

    async def serve(slave_id: str, answer: bool):
        while True:
            cmd = await bus.next_command(slave_id, 5)
            if cmd.kind != CommandKind.run:
                continue
            if not answer:  # swallow the command and die
                with get_session() as s:
                    repo.get_slave(s, slave_id).last_heartbeat = datetime.now(timezone.utc) - timedelta(minutes=5)
                continue
            await bus.deliver(CommandResultUpload(command_id=cmd.command_id, result=RunResult(
                run_id=cmd.run_spec.run_id, status=RunStatus.evaluated, metrics={"m": 1.0})))

    tasks = [asyncio.create_task(serve(dead, False)), asyncio.create_task(serve(alive, True))]
    try:
        req = RunRequest(run_id=new_id(), project_id=pid, repo=RepoRef(kind="git", url="file:///x", sha="a" * 40))
        res, slave_id = await asyncio.wait_for(ctx.dispatcher.run(req, timeout=20), 20)
        assert res.status == RunStatus.evaluated and slave_id == alive
    finally:
        for t in tasks:
            t.cancel()
        dispatch_mod.WAIT_SLICE_S = 5.0


async def test_dispatcher_reports_no_runner(db):
    with get_session() as s:
        pid = repo.create_project(s, ProjectCreate(name="g", repo_url="file:///x")).project_id
    ctx = MasterContext(settings=db, bus=CommandBus(), ws=WsBroker(), store=None)
    ctx.dispatcher.wait_for_runner_s = 0.1
    res, slave_id = await ctx.dispatcher.run(RunRequest(run_id=new_id(), project_id=pid))
    assert slave_id is None and res.error_kind == "infra" and "no online runner" in res.error


async def test_cron_schedule_fires_once_per_tick(db, tmp_path):
    ctx = MasterContext(settings=db, bus=CommandBus(), ws=WsBroker(), store=LocalArtifactStore(str(tmp_path / "a")))
    ctx.dispatcher.wait_for_runner_s = 0.1
    now = datetime.now().astimezone()
    with get_session() as s:
        row = repo.create_schedule(s, name="nightly", kind="cron", cron="*/5 * * * *", interval_s=300,
                                   scope=None, enabled=True)
        row.created_at = (now - timedelta(minutes=11)).astimezone(timezone.utc)
        sid = row.schedule_id
    sched = Scheduler(ctx)
    started = await sched.tick(now)
    assert len(started) == 1
    assert await sched.tick(now) == []  # already fired for this tick
    with get_session() as s:
        row = repo.get_schedule(s, sid)
        assert repo.aware(row.next_fire_at) > now and row.last_benchmark_id == started[0]
        assert repo.get_benchmark(s, started[0]).trigger == "schedule"
    await asyncio.gather(*list(ctx.tasks), return_exceptions=True)  # no projects => error status
    with get_session() as s:
        assert repo.get_benchmark(s, started[0]).status == "error"


def test_repo_path_parsing():
    assert repo_path("https://github.com/surrealier/ALGOps.git") == "surrealier/ALGOps"
    assert repo_path("git@github.com:surrealier/ALGOps.git") == "surrealier/ALGOps"
    assert repo_path("https://gitlab.corp/vision/team/detector") == "vision/team/detector"
    assert repo_path("ssh://git@gitlab.corp:2222/vision/det.git") == "vision/det"


async def test_open_pull_request_github_and_gitlab():
    seen: list[tuple[str, str, dict]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else {}
        seen.append((request.method, request.url.raw_path.decode(), body))
        if request.url.path == "/repos/o/r" and request.method == "GET":
            return httpx.Response(200, json={"default_branch": "main"})
        if request.url.path == "/repos/o/r/pulls":
            return httpx.Response(201, json={"html_url": "https://github.com/o/r/pull/7", "number": 7})
        if request.url.path.endswith("/merge_requests"):
            return httpx.Response(201, json={"web_url": "https://gl/g/r/-/merge_requests/3", "iid": 3})
        return httpx.Response(404)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    gh = MasterSettings(git_provider="github", git_provider_token="t", git_provider_api_url="https://api.gh")
    pr = await open_pull_request(gh, repo_url="git@github.com:o/r.git", branch="algops/x", base=None,
                                 title="T", body="B", client=client)
    assert pr["url"].endswith("/pull/7") and seen[-1][2]["base"] == "main" and seen[-1][2]["head"] == "algops/x"
    gl = MasterSettings(git_provider="gitlab", git_provider_token="t", git_provider_api_url="https://gl/api/v4")
    mr = await open_pull_request(gl, repo_url="https://gl/g/r.git", branch="algops/x", base="develop",
                                 title="T", body="B", client=client)
    assert mr["number"] == 3 and seen[-1][2]["target_branch"] == "develop"
    assert "/projects/g%2Fr/merge_requests" in seen[-1][1]  # GitLab needs the encoded path id
    none = await open_pull_request(MasterSettings(), repo_url="x", branch="b", base=None, title="t", body="b")
    assert "not configured" in none["error"]
    await client.aclose()
    assert os.environ.get("NO_SUCH_VAR") is None
