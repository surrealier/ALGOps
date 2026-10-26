"""Regression tests for the v2 hardening pass (dispatch, runner hygiene, auth, verdicts, promotion)."""

from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import httpx
import pytest

from algops.core import (
    CommandKind,
    CommandResultUpload,
    Constraint,
    EnvSpec,
    GoalDirection,
    MasterSettings,
    NextCommandResponse,
    Objective,
    ParamSpec,
    ProjectCreate,
    PromoteIn,
    PromoteResult,
    RepoRef,
    RunRequest,
    RunResult,
    RunStatus,
    new_id,
)
from algops.master.bus import CommandBus
from algops.master.context import MasterContext
from algops.master.ws import WsBroker
from algops.tracking import get_session, init_engine, repo
from algops.tracking.db import reset_engine

from .conftest import EXAMPLES, git

SDK_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "src", "algops", "sdk", "algops_sdk.py")


@pytest.fixture
def db(tmp_path):
    reset_engine()
    settings = MasterSettings(db_url=f"sqlite+pysqlite:///{tmp_path / 't.sqlite'}",
                              artifact_dir=str(tmp_path / "a"), run_retries=1, scheduler_enabled=False)
    init_engine(settings)
    yield settings
    reset_engine()


@pytest.fixture
def fast_slices():
    from algops.master import dispatch as dispatch_mod

    old = dispatch_mod.WAIT_SLICE_S
    dispatch_mod.WAIT_SLICE_S = 0.05
    yield
    dispatch_mod.WAIT_SLICE_S = old


def _ctx(settings, bus=None, **kw) -> MasterContext:
    return MasterContext(settings=settings, bus=bus or CommandBus(), ws=WsBroker(), store=None, **kw)


# --------------------------------------------------------------------- dispatcher + bus


async def test_timed_out_run_is_not_dispatched_twice(db, fast_slices):
    with get_session() as s:
        proj = repo.create_project(s, ProjectCreate(name="g", repo_url="file:///x"))
        a, _ = repo.register_slave(s, host_label="a")
        b, _ = repo.register_slave(s, host_label="b")
        pid, ids = proj.project_id, (a.slave_id, b.slave_id)
    bus = CommandBus()
    ctx = _ctx(db, bus)
    task = asyncio.create_task(ctx.dispatcher.run(RunRequest(run_id=new_id(), project_id=pid), timeout=0.2))
    got = []

    async def collect():
        for sid in ids:
            if (await bus.next_command(sid, 0.01)).kind == CommandKind.run:
                got.append(sid)

    await asyncio.sleep(0.05)
    await collect()  # one runner picks it up and never answers (still running)
    await asyncio.sleep(0.4)
    await collect()
    res, _ = await task
    assert len(got) == 1, got
    assert res.error_kind == "timeout"


async def test_queue_wait_does_not_count_against_the_run_timeout(db, fast_slices):
    with get_session() as s:
        proj = repo.create_project(s, ProjectCreate(name="g", repo_url="file:///x"))
        sl, _ = repo.register_slave(s, host_label="only", concurrency=1)
        pid, sid = proj.project_id, sl.slave_id
    bus = CommandBus()
    ctx = _ctx(db, bus)

    async def one_worker():  # each run takes 0.3 s; the second waits in the queue meanwhile
        while True:
            cmd = await bus.next_command(sid, 1.0)
            if cmd.kind != CommandKind.run:
                continue
            await asyncio.sleep(0.3)
            with get_session() as s:
                repo.touch_heartbeat(s, sid)
            await bus.deliver(CommandResultUpload(command_id=cmd.command_id, result=RunResult(
                run_id=cmd.run_spec.run_id, status=RunStatus.evaluated, metrics={"f1": 1.0})))

    worker = asyncio.create_task(one_worker())
    try:
        runs = [ctx.dispatcher.run(RunRequest(run_id=new_id(), project_id=pid), timeout=0.45) for _ in range(2)]
        results = await asyncio.gather(*runs)
    finally:
        worker.cancel()
    assert [r.status for r, _ in results] == [RunStatus.evaluated, RunStatus.evaluated]


async def test_bus_forget_is_final():
    bus = CommandBus()
    cid = await bus.enqueue("s1", NextCommandResponse(kind=CommandKind.run))
    await bus.forget(cid)
    assert await bus.wait(cid, 0.01) is None
    assert cid not in bus._results  # wait() never resurrects a forgotten command
    assert not bus._events["s1"].is_set()  # no spurious wake-up with an empty queue
    assert await bus.deliver(CommandResultUpload(command_id=cid)) is False


# --------------------------------------------------------------------- runner: workspace + env setup


MANIFEST = """\
algops: 2
name: tiny
env:
  setup: ["touch built.flag"]
suites:
  - { id: s, uri: "" }
algorithms:
  - id: a
    params:
      - { key: a.x, dtype: int, default: 1, range: [0, 5], step: 1, stage: s }
    entrypoint:
      command: "test -f built.flag && echo '::algops-result::{\\"metrics\\": {\\"f1\\": 1}}'"
    metrics: { declare: [{ name: f1, direction: maximize }] }
"""


def _tiny_repo(tmp_path, n=3):
    src = tmp_path / "src"
    src.mkdir()
    git("init", "-q", "-b", "main", cwd=str(src))
    (src / ".gitignore").write_text("built.flag\n")
    (src / "algops.yaml").write_text(MANIFEST)
    shas = []
    for i in range(n):
        (src / "v.txt").write_text(str(i))
        git("add", "-A", cwd=str(src))
        git("commit", "-q", "-m", f"c{i}", cwd=str(src))
        shas.append(git("rev-parse", "HEAD", cwd=str(src)))
    return "file://" + str(src), shas


async def test_recreated_worktree_is_set_up_again(tmp_path):
    from algops.runner.engine import RunnerEngine

    url, shas = _tiny_repo(tmp_path)
    eng = RunnerEngine(str(tmp_path / "w"), keep_checkouts=1)

    async def run(sha):
        req = RunRequest(run_id=new_id(), project_id="p", algorithm_id="a", suite_id="s",
                         repo=RepoRef(kind="git", name="tiny", url=url, sha=sha))
        return (await eng.run(req)).result

    assert (await run(shas[0])).status == RunStatus.evaluated
    assert (await run(shas[1])).status == RunStatus.evaluated  # prunes shas[0]'s worktree
    again = await run(shas[0])  # re-created at the same path: env.setup must run again
    assert again.status == RunStatus.evaluated, again


async def test_checkout_is_held_until_released(tmp_path):
    from algops.runner.workspace import Workspace

    url, shas = _tiny_repo(tmp_path)
    ws = Workspace(str(tmp_path / "ws"), keep_checkouts=1)
    co0 = await ws.checkout(url, shas[0])  # returned acquired
    co1 = await ws.checkout(url, shas[1])
    assert os.path.isdir(co0.path), "an acquired checkout must survive pruning"
    ws.release(co0.path)
    ws.release(co1.path)
    async with ws.use(url, shas[2]) as co2:
        assert os.path.isdir(co2.path)
    assert not os.path.isdir(co0.path) and not os.path.isdir(co1.path)


async def test_repo_code_does_not_see_runner_secrets(tmp_path, monkeypatch):
    from algops.runner.engine import RunnerEngine
    from algops.runner.process import EnvPolicy

    monkeypatch.setenv("ALGOPS_SLAVE_ENROLL_TOKEN", "placeholder")
    monkeypatch.setenv("SOME_SERVICE_TOKEN", "placeholder")
    monkeypatch.setenv("DATASET_ROOT_FOR_TEST", "/data")
    src = tmp_path / "loc"
    src.mkdir()
    probe = ('test -z "$ALGOPS_SLAVE_ENROLL_TOKEN" && test -z "$SOME_SERVICE_TOKEN" '
             '&& test -n "$DATASET_ROOT_FOR_TEST" && echo')
    (src / "algops.yaml").write_text(MANIFEST.replace("touch built.flag", "true")
                                     .replace("test -f built.flag && echo", probe.replace('"', '\\"')))
    req = lambda: RunRequest(run_id=new_id(), project_id="p", algorithm_id="a", suite_id="s",  # noqa: E731
                             repo=RepoRef(kind="local", name="tiny"))
    eng = RunnerEngine(str(tmp_path / "w"), local_projects={"tiny": str(src)})
    assert (await eng.run(req())).result.status == RunStatus.evaluated
    # explicitly re-allowed => visible again (the probe now fails)
    eng2 = RunnerEngine(str(tmp_path / "w2"), local_projects={"tiny": str(src)},
                        env_policy=EnvPolicy(pass_env=("SOME_SERVICE_*",)))
    assert (await eng2.run(req())).result.status == RunStatus.failed
    assert EnvPolicy().allowed("MAX_THINKING_TOKENS")  # a count, not a credential


async def test_local_run_refuses_code_that_changed_since_sync(tmp_path):
    from algops.runner.engine import RunnerEngine

    work = tmp_path / "proj"
    shutil.copytree(os.path.join(EXAMPLES, "py-multi-algo"), work,
                    ignore=shutil.ignore_patterns("build", "__pycache__", ".algops"))
    eng = RunnerEngine(str(tmp_path / "w"), local_projects={"p": str(work)})
    req = RunRequest(run_id=new_id(), project_id="p", algorithm_id="detector", suite_id="det-smoke",
                     repo=RepoRef(kind="local", name="p", sha="deadbeef" * 5))
    res = (await eng.run(req)).result
    assert res.status == RunStatus.failed and res.error_kind == "setup"
    assert "changed since it was synced" in res.error


def test_git_url_and_ref_validation():
    from algops.core.schemas import check_git_ref, check_git_url

    for ok in ("main", "release/2026.10", "v1.2.3", "a" * 40):
        assert check_git_ref(ok) == ok
    for bad in ("-x", "--upload-pack=sh", "a..b", "x^{commit}", "@{u}", "a b"):
        with pytest.raises(ValueError):
            check_git_ref(bad)
    for ok in ("https://github.com/a/b.git", "git@github.com:a/b.git", "file:///srv/x.git"):
        assert check_git_url(ok) == ok
    for bad in ("--upload-pack=touch x", "ext::sh -c x", "fd::3"):
        with pytest.raises(ValueError):
            check_git_url(bad)
    with pytest.raises(ValueError):
        ProjectCreate(name="x", repo_url="-oProxyCommand=x")


# --------------------------------------------------------------------- artifacts + telemetry


def test_artifact_store_is_confined(tmp_path):
    from algops.tracking.artifacts import ArtifactKeyError, LocalArtifactStore

    st = LocalArtifactStore(str(tmp_path / "store"))
    (tmp_path / "sibling.txt").write_text("outside")
    for bad in ("algops://../sibling.txt", "../sibling.txt", "algops://runs/../../sibling.txt",
                "s3://bucket/x", "algops://"):
        with pytest.raises(ArtifactKeyError):
            st.get_text(bad)
    uri = st.put_text("runs/r1/log.txt", "hi")
    assert uri == "algops://runs/r1/log.txt" and st.get_text(uri) == "hi"


def test_telemetry_stage_files_stay_next_to_their_manifest(tmp_path):
    from algops.extraction import resolve_sibling_uri
    from algops.master.hooks import DbHooks
    from algops.tracking.artifacts import LocalArtifactStore

    assert resolve_sibling_uri("algops://runs/r1/out/manifest.json", "decision.csv") == \
        "algops://runs/r1/out/decision.csv"
    for bad in ("../../other/x.csv", "/etc/hosts", "a/../../b.csv"):
        with pytest.raises(ValueError):
            resolve_sibling_uri("algops://runs/r1/out/manifest.json", bad)
    hooks = DbHooks.__new__(DbHooks)
    hooks.store = LocalArtifactStore(str(tmp_path / "s"))
    (tmp_path / "secret.txt").write_text("x")
    assert hooks.fetch_text(str(tmp_path / "secret.txt")) is None  # never reads master files


# --------------------------------------------------------------------- verdicts + baselines


def _algo(**extra):
    from algops.core.manifest import AlgorithmSpec

    data = {"id": "a", "entrypoint": {"command": ["true"]}, "metrics": {"presets": ["binary"]}}
    data.update(extra)
    return AlgorithmSpec.model_validate(data)


def test_verdict_noise_floor_and_lost_metrics():
    from algops.extraction.verdict import judge

    algo = _algo(objective={"metric": "fp_rate"})
    assert judge(algo, True, {"fp_rate": 1e-12}, {"fp_rate": 0.0})[0].value == "passed"
    assert judge(algo, True, {"fp_rate": 0.01}, {"fp_rate": 0.0})[0].value == "regressed"
    v, _, d = judge(_algo(objective={"metric": "recall"}), True, {"precision": 0.9}, {"recall": 0.95})
    assert d["recall"]["status"] == "lost" and v.value == "regressed"
    v, _, d = judge(_algo(objective={"metric": "recall"}), True, {"recall": 0.9}, {"precision": 0.9})
    assert d["recall"]["status"] == "new" and v.value == "passed"


def test_none_metrics_are_sql_null_and_error_cells_are_never_a_baseline(db):
    from sqlalchemy import select

    from algops.tracking.models import BenchmarkCell

    with get_session() as s:
        p = repo.create_project(s, ProjectCreate(name="g", repo_url="file:///x"))
        b = repo.create_benchmark(s, name="b", trigger="manual", scope=None, baseline="previous")
        c = repo.add_cell(s, benchmark_id=b.benchmark_id, project_id=p.project_id,
                          algorithm_id="a", suite_id="s", commit_sha=None)
        c.metrics = None
        c.verdict = "error"
        pid, bid = p.project_id, b.benchmark_id
    with get_session() as s:
        assert list(s.scalars(select(BenchmarkCell).where(BenchmarkCell.metrics.is_not(None)))) == []
        assert repo.baseline_cell(s, project_id=pid, algorithm_id="a", suite_id="s",
                                  exclude_benchmark_id="other", baseline=bid) is None


# --------------------------------------------------------------------- search


def test_random_counts_only_its_own_subspace():
    from algops.search import propose

    spec = ParamSpec(params=[
        {"key": "a.x", "dtype": "bool", "default": False, "stage": "s"},
        {"key": "a.y", "dtype": "int", "default": 0, "range": [0, 9], "step": 1, "stage": "s"},
    ])
    obj = Objective(metric="f1", direction=GoalDirection.maximize)
    base = spec.effective()
    hist = [{"params": {**base, "a.y": y}, "status": "evaluated", "metrics": {"f1": 0.1}} for y in (5, 6)]
    out, _, conv = propose("random", spec, ["a.x"], obj, hist, {}, 4, base=base)
    assert sorted(c["a.x"] for c in out) == [False, True] and not conv
    hist += [{"params": c, "status": "evaluated", "metrics": {"f1": 0.1}} for c in out]
    out, _, conv = propose("random", spec, ["a.x"], obj, hist, {}, 4, base=base)
    assert out == [] and conv


def test_grid_includes_the_range_upper_bound():
    spec = ParamSpec(params=[{"key": "a.f", "dtype": "float", "default": 0.0, "range": [0, 1],
                              "step": 0.3, "stage": "s"}])
    g = spec.by_key()["a.f"].grid()
    assert g[0] == 0.0 and g[-1] == 1.0


def test_constraint_rank_still_feasibility_first():
    obj = Objective(metric="f1", direction=GoalDirection.maximize,
                    constraints=[Constraint(metric="recall", op=">=", value=0.9)])
    assert obj.rank({"f1": 0.5, "recall": 0.95}) < obj.rank({"f1": 0.99, "recall": 0.5})


# --------------------------------------------------------------------- campaign loop


async def test_llm_cannot_claim_the_target_was_reached():
    from algops.agents.hooks import NullHooks
    from algops.agents.nodes import LoopDeps, make_nodes
    from algops.agents.state import initial_state
    from algops.llm.planner import Decision

    class Planner:
        async def decide(self, state):
            return Decision(action="stop", stop_reason="target_reached")

    nodes = make_nodes(LoopDeps(executor=None, hooks=NullHooks(), planner=Planner()))
    st = initial_state(experiment_id="e", project_id="p", project_name="p", goal_text="",
                       target_metric="f1", target_value=0.99, goal_direction="maximize",
                       max_iteration=5, param_spec={"params": []})
    st["iteration"] = 1
    st["history"] = [{"iteration": 1, "run_id": "r", "kind": "search", "params": {},
                      "status": "evaluated", "metrics": {"f1": 0.5}, "objective": 0.5}]
    out = await nodes["pm_decide"](st)
    assert out["stop_reason"] == "pm_stop"


# --------------------------------------------------------------------- promotion


def test_promotion_writes_the_declared_dtype(tmp_path):
    from algops.runner.promote import rewrite_defaults

    p = tmp_path / "params.yaml"
    p.write_text("params:\n  - key: det.scale\n    dtype: float\n    default: 1   # tuned\n"
                 "    range: [0, 4]\n    step: 0.5\n    stage: s\n")
    changed = rewrite_defaults(str(p), "a", False, {"det.scale": 1.5})
    assert changed["det.scale"]["new"] == 1.5
    assert "default: 1.5 # tuned" in p.read_text()  # comment kept in its column


async def test_promotion_uses_the_commit_the_result_ran_on(db):
    from algops.master.services.promotion import promote_experiment
    from algops.tracking.artifacts import LocalArtifactStore

    with get_session() as s:
        p = repo.create_project(s, ProjectCreate(name="g", repo_url="file:///x"))
        e = repo.create_experiment(s, project_id=p.project_id, name="e", goal_text="",
                                   target_metric="f1", target_value=None, goal_direction="maximize",
                                   max_iteration=2, algorithm_id="a")
        eid = e.experiment_id
        e.commit_sha = "1" * 40
        repo.upsert_final_result(s, eid, None, {"a.x": 2}, {"f1": 0.9}, "max_iteration", None,
                                 attempt=0, changed_params={"a.x": {"old": 1, "new": 2}}, commit_sha="1" * 40)
    with get_session() as s:  # a re-run pins a newer commit
        repo.start_attempt(s, eid)
        repo.get_experiment(s, eid).commit_sha = "2" * 40
    seen = {}

    class FakeDispatcher:
        async def promote(self, req):
            seen["req"] = req
            return PromoteResult(ok=False, error="fake")

    ctx = MasterContext(settings=db, bus=CommandBus(), ws=WsBroker(),
                        store=LocalArtifactStore(db.artifact_dir), dispatcher=FakeDispatcher())
    await promote_experiment(ctx, eid, PromoteIn())
    req = seen["req"]
    assert req.repo.sha == "1" * 40
    assert req.branch.endswith(eid.replace("-", "")[-12:] + "-a0")


# --------------------------------------------------------------------- scheduler + startup


async def test_watch_schedule_honors_the_scope_ref(db, monkeypatch):
    from algops.master.services import scheduler as sched_mod

    refs = []

    class Rev:
        sha = "a" * 40

    async def fake_sync(ctx, pid, ref):
        refs.append(ref)
        return Rev(), None

    monkeypatch.setattr(sched_mod, "sync_project", fake_sync)
    with get_session() as s:
        p = repo.create_project(s, ProjectCreate(name="g", repo_url="file:///x"))
        repo.create_schedule(s, name="w", kind="watch", cron=None, interval_s=30,
                             scope=[{"project_id": p.project_id, "ref": "release"}], enabled=True)
    await sched_mod.Scheduler(_ctx(db)).tick()
    assert refs == ["release"]


def test_dev_mode_restart_reconciles_orphaned_work(db):
    with get_session() as s:
        p = repo.create_project(s, ProjectCreate(name="g", repo_url="file:///x"))
        e = repo.create_experiment(s, project_id=p.project_id, name="e", goal_text="",
                                   target_metric="f1", target_value=None, goal_direction="maximize",
                                   max_iteration=2, algorithm_id="a")
        e.status = "running"
        b = repo.create_benchmark(s, name="b", trigger="manual", scope=None, baseline="previous")
        b.status = "running"
        repo.add_cell(s, benchmark_id=b.benchmark_id, project_id=p.project_id, algorithm_id="a",
                      suite_id="s", commit_sha=None)
        eid, bid = e.experiment_id, b.benchmark_id
    with get_session() as s:
        assert repo.reconcile_orphans(s) == {"experiments": 1, "benchmarks": 1, "cells": 1}
    with get_session() as s:
        assert repo.get_experiment(s, eid).status == "failed"
        assert repo.get_benchmark(s, bid).status == "error"


# --------------------------------------------------------------------- API auth


def test_api_token_guards_reads_websockets_and_artifacts(tmp_path):
    from fastapi.testclient import TestClient
    from starlette.websockets import WebSocketDisconnect

    from algops.master.app import create_app

    reset_engine()
    settings = MasterSettings(db_url=f"sqlite+pysqlite:///{tmp_path / 't.sqlite'}",
                              artifact_dir=str(tmp_path / "a"), api_token="tok", scheduler_enabled=False)
    try:
        with TestClient(create_app(settings)) as c:
            assert c.get("/healthz").json()["auth"] is True
            assert c.get("/api/v1/projects").status_code == 401
            assert c.get("/api/v1/slaves").status_code == 401
            assert c.get("/api/v1/llm/probe").status_code == 401
            assert c.get("/api/v1/projects", headers={"Authorization": "Bearer tok"}).status_code == 200
            assert c.get("/api/v1/projects", params={"token": "tok"}).status_code == 200
            assert c.get("/api/v1/artifacts", params={"uri": "algops://../t.sqlite", "token": "tok"}).status_code == 404
            with pytest.raises(WebSocketDisconnect):
                with c.websocket_connect("/ws/benchmarks/x") as ws:
                    ws.receive_text()
            with c.websocket_connect("/ws/benchmarks/x?token=tok"):
                pass
            bad = c.post("/api/v1/slaves/register", json={"enroll_token": "nope", "host_label": "h"})
            assert bad.status_code == 401
    finally:
        reset_engine()


def test_production_master_refuses_the_default_enroll_token(tmp_path):
    from algops.master.app import create_app

    reset_engine()
    try:
        with pytest.raises(RuntimeError, match="ENROLL_TOKEN"):
            create_app(MasterSettings(db_url=f"sqlite+pysqlite:///{tmp_path / 't.sqlite'}",
                                      dev_mode=False, scheduler_enabled=False))
    finally:
        reset_engine()


# --------------------------------------------------------------------- algops ci


def _ci_client(handler):
    return httpx.Client(base_url="http://m", transport=httpx.MockTransport(handler))


def test_ci_survives_transient_errors_while_polling():
    from algops.ci import run_ci

    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json={"benchmark_id": "b1"})
        calls["n"] += 1
        if calls["n"] < 3:
            return httpx.Response(502, text="<html>Bad Gateway</html>")
        return httpx.Response(200, json={"status": "passed", "cells": [], "summary": {"passed": 1}})

    code = run_ci(master_url="http://m", token=None, project=None, ref=None, algorithms=None,
                  suites=None, tags=None, poll_s=0.0, client=_ci_client(handler))
    assert code == 0


def test_ci_persistent_api_failure_is_exit_2_not_a_failed_verdict():
    from algops.ci import run_ci

    def handler(req: httpx.Request) -> httpx.Response:
        if req.method == "POST":
            return httpx.Response(200, json={"benchmark_id": "b1"})
        return httpx.Response(200, text="<html>login page</html>")

    code = run_ci(master_url="http://m", token=None, project=None, ref=None, algorithms=None,
                  suites=None, tags=None, poll_s=0.0, client=_ci_client(handler))
    assert code == 2


# --------------------------------------------------------------------- SDK


def test_sdk_metric_named_error_is_a_metric(tmp_path):
    spec = importlib.util.spec_from_file_location("algops_sdk_hardening", SDK_PATH)
    sdk = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sdk)
    ctx = sdk.Context({"output_dir": str(tmp_path)})
    res = sdk._normalize({"error": 0.12, "mae": 0.10}, ctx)
    assert res["metrics"] == {"error": 0.12, "mae": 0.10} and res["status"] == "ok"
    res = sdk._normalize({"error": "boom"}, ctx)
    assert res["status"] == "failed"


async def test_env_setup_marker_tracks_checkout_identity(tmp_path):
    from algops.runner.envsetup import EnvSetup

    es = EnvSetup(str(tmp_path / "work"))
    co = tmp_path / "co"
    co.mkdir()
    (co / ".git").write_text("gitdir: /x\n")
    env = EnvSpec(setup=["touch built.flag"])
    assert (await es.ensure(str(co), "a" * 40, env)).ok
    assert (await es.ensure(str(co), "a" * 40, env)).cached
    shutil.rmtree(co)  # pruned, then re-created at the same path
    co.mkdir()
    (co / ".git").write_text("gitdir: /y\n")
    r = await es.ensure(str(co), "a" * 40, env)
    assert not r.cached and (co / "built.flag").exists()
