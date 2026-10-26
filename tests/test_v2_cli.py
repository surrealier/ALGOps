"""The ``algops`` CLI: init → validate → run → test (+ baseline), verify, and ``algops ci``."""

from __future__ import annotations

import json
import os

import httpx

from algops.ci import run_ci
from algops.cli import main

from .conftest import EXAMPLES


def test_init_validate_run_test_and_baseline(tmp_path, capsys):
    repo = str(tmp_path / "proj")
    work = ["--work-dir", str(tmp_path / "w")]
    assert main(["init", repo, "-a", "detector"]) == 0
    for f in ("algops.yaml", "algops/detector/params.yaml", "algops/detector/run.py", "AGENTS.md"):
        assert os.path.isfile(os.path.join(repo, f)), f
    assert main(["validate", repo]) == 0
    assert main(["verify", repo, *work]) == 0
    assert main(["run", repo, "--set", "detector.confidence_threshold=0.6", *work]) == 0
    assert main(["run", repo, "--set", "detector.nope=1", *work]) == 1  # unknown param
    report = str(tmp_path / "base.json")
    assert main(["test", repo, "--json", report, *work]) == 0
    doc = json.load(open(report, encoding="utf-8"))
    assert doc["ok"] and doc["cells"][0]["verdict"] == "passed"
    # Lower the default -> more false positives -> a regression against the saved report.
    p = os.path.join(repo, "algops", "detector", "params.yaml")
    text = open(p, encoding="utf-8").read().replace("default: 0.5", "default: 0.2")
    open(p, "w", encoding="utf-8").write(text)
    capsys.readouterr()
    assert main(["test", repo, "--baseline", report, *work]) == 1
    assert "regressed" in capsys.readouterr().out


def test_examples_pass_their_gates(tmp_path):
    work = ["--work-dir", str(tmp_path / "w")]
    assert main(["test", os.path.join(EXAMPLES, "py-multi-algo"), "-j", "4", "-q", *work]) == 0
    assert main(["test", os.path.join(EXAMPLES, "py-multi-algo"), "-t", "fast", "-q", *work]) == 0


def test_validate_reports_broken_manifest(tmp_path):
    (tmp_path / "algops.yaml").write_text("algops: 2\nalgorithms:\n  - id: a\n    entrypoint: {python: missing.py:run}\n",
                                         encoding="utf-8")
    assert main(["validate", str(tmp_path)]) == 1
    (tmp_path / "algops.yaml").write_text("algops: 2\nalgorithms: []\n", encoding="utf-8")
    assert main(["validate", str(tmp_path)]) == 1


def test_ci_command_against_a_mock_master(capsys):
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/api/v1/projects/vision":
            return httpx.Response(200, json={"project_id": "pid-1"})
        if request.method == "POST" and request.url.path == "/api/v1/benchmarks":
            body = json.loads(request.content)
            assert body["scope"] == [{"project_id": "pid-1", "ref": "abc123"}] and body["trigger"] == "api"
            assert request.headers["authorization"] == "Bearer tok"
            return httpx.Response(200, json={"benchmark_id": "b1"})
        if request.url.path == "/api/v1/benchmarks/b1":
            calls["n"] += 1
            if calls["n"] < 2:
                return httpx.Response(200, json={"status": "running", "cells": []})
            return httpx.Response(200, json={"status": "failed", "summary": {"regressed": 1}, "cells": [{
                "verdict": "regressed", "project_id": "pid-1", "project_name": "vision", "algorithm_id": "det",
                "suite_id": "smoke", "metrics": {"fp_rate": 0.4}, "gates": [],
                "deltas": {"fp_rate": {"status": "regressed", "baseline": 0.3, "current": 0.4}}}]})
        return httpx.Response(404)

    client = httpx.Client(base_url="http://m", transport=httpx.MockTransport(handler),
                          headers={"Authorization": "Bearer tok"})
    rc = run_ci(master_url="http://m", token="tok", project=["vision"], ref="abc123", algorithms=None,
                suites=None, tags=None, poll_s=0.01, client=client)
    out = capsys.readouterr().out
    assert rc == 1 and "regressed fp_rate: 0.3 → 0.4" in out
    assert run_ci(master_url="http://m", token=None, project=None, ref="x", algorithms=None, suites=None,
                  tags=None) == 2  # --ref without --project
