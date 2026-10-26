"""Real distributed M0 flow: master HTTP server + slave poller in separate event loops.

Proves the slave-initiated path end to end — register -> online -> experiment -> command-bus
dispatch -> slave run + artifact upload -> result -> checkpointed loop -> target_reached.
"""

from __future__ import annotations

import asyncio
import os
import socket
import tempfile
import threading

import httpx
import uvicorn

from algops.core import MasterSettings, SlaveSettings
from algops.master import create_app
from algops.slave import run_slave
from algops.tracking.db import reset_engine


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


async def _wait(cond, timeout=60.0, interval=0.2):
    waited = 0.0
    while waited < timeout:
        result = await cond()
        if result:
            return result
        await asyncio.sleep(interval)
        waited += interval
    return None


async def test_distributed_end_to_end():
    tmp = tempfile.mkdtemp(prefix="algops_e2e_")
    port = _free_port()
    base = f"http://127.0.0.1:{port}"
    reset_engine()
    settings = MasterSettings(
        db_url=f"sqlite+pysqlite:///{os.path.join(tmp, 'tracking.sqlite')}",
        artifact_dir=os.path.join(tmp, "artifacts"),
        checkpoint_db=os.path.join(tmp, "checkpoints.sqlite"),
        host="127.0.0.1",
        port=port,
        dev_mode=True,
        enroll_token="e2e-token",
    )
    app = create_app(settings)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    slave_task: asyncio.Task | None = None
    try:
        assert await _wait(lambda: asyncio.sleep(0, result=server.started), timeout=20)

        slave_task = asyncio.create_task(
            run_slave(
                SlaveSettings(
                    master_url=base,
                    enroll_token="e2e-token",
                    project_name="toy-e2e",
                    work_dir=os.path.join(tmp, "slave_work"),
                )
            )
        )

        async with httpx.AsyncClient(base_url=base, timeout=10.0) as c:
            assert (await c.get("/healthz")).json()["status"] == "ok"

            async def project_online():
                ps = (await c.get("/api/v1/projects")).json()
                proj = next((p for p in ps if p["name"] == "toy-e2e"), None)
                return proj if (proj and proj["slave_online"]) else None

            proj = await _wait(project_online, timeout=30)
            assert proj, "slave did not register / come online"

            exp = (
                await c.post(
                    "/api/v1/experiments",
                    json={
                        "project_id": proj["project_id"],
                        "goal_text": "오탐 완화",
                        "target_metric": "fp_rate",
                        "target_value": 0.1,
                        "max_iteration": 10,
                    },
                )
            ).json()
            eid = exp["experiment_id"]
            assert exp["goal_direction"] == "minimize"

            await c.post(f"/api/v1/experiments/{eid}/run")

            async def terminal():
                e = (await c.get(f"/api/v1/experiments/{eid}")).json()
                return e if e["status"] in (
                    "target_reached", "stopped_max_iter", "stopped_by_pm", "failed"
                ) else None

            done = await _wait(terminal, timeout=60)
            assert done, "experiment did not finish in time"
            assert done["status"] == "target_reached", done

            final = (await c.get(f"/api/v1/experiments/{eid}/final")).json()
            assert final["metrics"]["fp_rate"] <= 0.1
            assert final["notified"] is True
            runs = (await c.get(f"/api/v1/experiments/{eid}/runs")).json()
            assert len(runs) >= 1
            # The winning run's CSV was uploaded by the slave through the master proxy.
            assert all(r["status"] == "evaluated" for r in runs)
    finally:
        if slave_task is not None:
            slave_task.cancel()
            try:
                await slave_task
            except (asyncio.CancelledError, Exception):
                pass
        server.should_exit = True
        thread.join(timeout=10)
        reset_engine()
