"""End to end over HTTP: master + a multi-repo runner, a git project, benchmarks with regression
verdicts and baseline acceptance, a constrained batch campaign with validation, promotion to the
git remote, attempts, API-token auth and the git-watch trigger."""

from __future__ import annotations

import asyncio
import os
import socket
import threading

import httpx
import uvicorn

from algops.core import MasterSettings, SlaveSettings
from algops.master import create_app
from algops.slave import run_slave
from algops.tracking.db import reset_engine

from .conftest import git

TERMINAL_EXP = {"target_reached", "stopped_max_iter", "stopped_by_pm", "failed"}


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _wait(cond, timeout=90.0, interval=0.25):
    waited = 0.0
    while waited < timeout:
        result = await cond()
        if result:
            return result
        await asyncio.sleep(interval)
        waited += interval
    return None


async def test_v2_platform_end_to_end(tmp_path, py_repo):
    tmp = str(tmp_path)
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    reset_engine()
    settings = MasterSettings(
        db_url=f"sqlite+pysqlite:///{os.path.join(tmp, 'tracking.sqlite')}",
        artifact_dir=os.path.join(tmp, "artifacts"),
        checkpoint_db=os.path.join(tmp, "checkpoints.sqlite"),
        host="127.0.0.1", port=port, dev_mode=True, enroll_token="e2e", api_token="s3cret",
        scheduler_enabled=False,
    )
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    runner: asyncio.Task | None = None
    auth = {"Authorization": "Bearer s3cret"}
    try:
        assert await _wait(lambda: asyncio.sleep(0, result=server.started), timeout=20)
        runner = asyncio.create_task(run_slave(SlaveSettings(
            master_url=base, enroll_token="e2e", concurrency=3, labels=["cpu"], serve_toy=False,
            work_dir=os.path.join(tmp, "runner"), heartbeat_interval_s=2.0,
        )))
        async with httpx.AsyncClient(base_url=base, timeout=30.0) as anon, \
                httpx.AsyncClient(base_url=base, timeout=120.0, headers=auth) as c:
            # ---- auth: with an API token every API call needs it (header, or ?token= for links)
            assert (await anon.post("/api/v1/projects", json={"name": "x", "repo_url": py_repo.url})).status_code == 401
            assert (await anon.get("/api/v1/projects")).status_code == 401
            assert (await anon.get("/api/v1/slaves")).status_code == 401
            assert (await anon.get("/api/v1/projects", params={"token": "wrong"})).status_code == 401
            assert (await anon.get("/api/v1/projects", params={"token": "s3cret"})).status_code == 200
            assert (await anon.get("/healthz")).json()["auth"] is True
            proj = (await c.post("/api/v1/projects", json={"name": "py", "repo_url": py_repo.url})).json()
            assert proj["repo_kind"] == "git"

            async def runner_online():
                rs = (await c.get("/api/v1/slaves")).json()
                return rs if rs and rs[0]["online"] and rs[0]["concurrency"] == 3 else None

            assert await _wait(runner_online, timeout=30), "runner did not come online"

            rev = (await c.post("/api/v1/projects/py/sync")).json()
            assert rev["sha"] == py_repo.head()
            p = (await c.get("/api/v1/projects/py")).json()
            assert p["algorithms"] == ["detector", "smoother"] and p["slave_online"]

            async def bench(**body) -> dict:
                b = (await c.post("/api/v1/benchmarks", json={"name": "b", **body})).json()

                async def done():
                    x = (await c.get(f"/api/v1/benchmarks/{b['benchmark_id']}")).json()
                    return x if x["status"] in ("passed", "failed", "error") else None

                out = await _wait(done, timeout=120)
                assert out, "benchmark did not finish"
                return out

            def verdicts(b: dict) -> dict[tuple[str, str], str]:
                return {(x["algorithm_id"], x["suite_id"]): x["verdict"] for x in b["cells"]}

            # ---- benchmark 1: every algorithm x suite of the repo, all gates pass
            b1 = await bench()
            assert b1["status"] == "passed", b1
            assert set(verdicts(b1).values()) == {"passed"} and len(b1["cells"]) == 4
            assert b1["revisions"][proj["project_id"]] == py_repo.head()

            # ---- a commit lowers the default threshold: smoke regresses, full fails a gate
            path = os.path.join(py_repo.work, "algops", "detector", "params.yaml")
            text = open(path, encoding="utf-8").read().replace("default: 0.5 ", "default: 0.45")
            open(path, "w", encoding="utf-8").write(text)
            bad_sha = py_repo.commit_all("lower threshold")
            b2 = await bench()
            v2 = verdicts(b2)
            assert b2["status"] == "failed"
            assert v2[("detector", "det-smoke")] == "regressed"
            assert v2[("detector", "det-full")] == "gate_failed"
            assert v2[("smoother", "sig-smoke")] == "passed"
            smoke = next(x for x in b2["cells"] if x["suite_id"] == "det-smoke")
            assert smoke["deltas"]["fp_rate"]["status"] == "regressed" and smoke["baseline_sha"] != bad_sha
            history = (await c.get("/api/v1/projects/py/history",
                                      params={"algorithm_id": "detector", "suite_id": "det-smoke"})).json()
            assert [h["commit_sha"] for h in history[:2]] == [bad_sha, b1["revisions"][proj["project_id"]]]

            # ---- regressions keep failing until fixed or accepted; accepting moves the baseline
            b3 = await bench(scope=[{"project_id": "py", "algorithms": ["detector"], "suites": ["det-smoke"]}])
            assert verdicts(b3) == {("detector", "det-smoke"): "regressed"}
            assert (await c.post(f"/api/v1/benchmarks/{b3['benchmark_id']}/accept")).json()["accepted"] == 1
            b4 = await bench(scope=[{"project_id": "py", "algorithms": ["detector"], "suites": ["det-smoke"]}])
            assert verdicts(b4) == {("detector", "det-smoke"): "passed"}

            # ---- campaign: constrained objective from the manifest, batch 3, held-out validation
            exp = (await c.post("/api/v1/experiments", json={
                "project_id": "py", "name": "tune detector", "goal_text": "오탐 완화",
                "algorithm_id": "detector", "suite_id": "det-smoke", "validation_suite_ids": ["det-full"],
                "batch_size": 3, "max_iteration": 10,
            })).json()
            assert exp["objective"]["metric"] == "fp_rate" and exp["objective"]["constraints"]
            eid = exp["experiment_id"]
            started = (await c.post(f"/api/v1/experiments/{eid}/run")).json()
            assert started["attempt"] == 0

            async def exp_done():
                e = (await c.get(f"/api/v1/experiments/{eid}")).json()
                return e if e["status"] in TERMINAL_EXP else None

            e = await _wait(exp_done, timeout=180)
            assert e and e["status"] != "failed", e
            assert e["commit_sha"] == bad_sha  # pinned to the revision resolved at start
            final = (await c.get(f"/api/v1/experiments/{eid}/final")).json()
            assert final["metrics"]["recall"] >= 0.7
            assert final["changed_params"], "the campaign should improve on the (regressed) defaults"
            assert final["validation"]["det-full"]["best"]["status"] == "evaluated"
            runs = (await c.get(f"/api/v1/experiments/{eid}/runs")).json()
            assert {r["kind"] for r in runs} == {"search", "validation"}
            assert all(r["commit_sha"] == bad_sha for r in runs)
            assert any(r["log_uri"] for r in runs)  # artifacts proxied through the master
            log = await c.get("/api/v1/artifacts", params={"uri": runs[0]["log_uri"]})
            assert log.status_code == 200
            assert (await anon.get("/api/v1/artifacts", params={"uri": runs[0]["log_uri"]})).status_code == 401
            outside = await c.get("/api/v1/artifacts", params={"uri": "algops://../tracking.sqlite"})
            assert outside.status_code == 404

            # ---- promotion: branch commit on the remote, patch stored, main untouched
            promo = (await c.post(f"/api/v1/experiments/{eid}/promote", json={"push": True})).json()
            assert promo["ok"] and promo["pushed"], promo
            assert git("--git-dir", py_repo.remote, "rev-parse", promo["branch"]) == promo["commit_sha"]
            assert git("--git-dir", py_repo.remote, "rev-parse", "main") == bad_sha
            patch = await c.get("/api/v1/artifacts", params={"uri": promo["patch_uri"]})
            assert "algops/detector/params.yaml" in patch.text
            assert (await c.get(f"/api/v1/experiments/{eid}/final")).json()["promotion"]["branch"] == promo["branch"]

            # ---- re-run = a new attempt with a clean thread (no history carry-over)
            again = (await c.post(f"/api/v1/experiments/{eid}/run")).json()
            assert again["attempt"] == 1
            assert await _wait(exp_done, timeout=180)
            runs1 = (await c.get(f"/api/v1/experiments/{eid}/runs")).json()
            all_runs = (await c.get(f"/api/v1/experiments/{eid}/runs", params={"all_attempts": "true"})).json()
            assert runs1 and {r["attempt"] for r in runs1} == {1}
            assert len(all_runs) == len(runs) + len(runs1)
            assert min(r["iteration_index"] for r in runs1) == 1

            # ---- git watch: a new commit on main triggers a benchmark of just that project
            sch = (await c.post("/api/v1/schedules", json={"name": "watch", "kind": "watch",
                                                           "scope": [{"project_id": "py"}]})).json()
            first = (await c.post(f"/api/v1/schedules/{sch['schedule_id']}/fire")).json()
            assert first["changed"] is False  # first observation only records the sha
            text = open(path, encoding="utf-8").read().replace("default: 0.45", "default: 0.5 ")
            open(path, "w", encoding="utf-8").write(text)
            fixed = py_repo.commit_all("restore threshold")
            second = (await c.post(f"/api/v1/schedules/{sch['schedule_id']}/fire")).json()
            assert second["changed"] and second["benchmark_id"]

            async def watch_done():
                x = (await c.get(f"/api/v1/benchmarks/{second['benchmark_id']}")).json()
                return x if x["status"] in ("passed", "failed", "error") else None

            wb = await _wait(watch_done, timeout=120)
            assert wb["trigger"] == "watch" and wb["revisions"][proj["project_id"]] == fixed
            assert len(wb["cells"]) == 4

            # ---- invalid inputs are rejected up front
            r = await c.post("/api/v1/experiments", json={"project_id": "py", "algorithm_id": "nope"})
            assert r.status_code == 400
            r = await c.post("/api/v1/schedules", json={"kind": "cron", "cron": "not a cron"})
            assert r.status_code == 400
    finally:
        if runner is not None:
            runner.cancel()
            try:
                await runner
            except (asyncio.CancelledError, Exception):
                pass
        server.should_exit = True
        thread.join(timeout=10)
        reset_engine()
