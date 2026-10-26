"""``algops test`` — run every algorithm × suite of one repository locally and judge the results.

No master, no network: the same engine and verdict rules as a master benchmark, so a repo's own
CI can gate merges (``algops test --baseline main-results.json``) and developers can reproduce a
failing cell exactly.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from typing import Any

from algops.core import (
    FAILING_VERDICTS,
    RepoRef,
    RunRequest,
    RunStatus,
    Verdict,
    load_manifest,
    new_id,
)
from algops.extraction.verdict import judge

from .engine import RunnerEngine
from .workspace import Workspace

REPORT_VERSION = 1


def default_work_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "algops", "work")


def _baseline_index(doc: dict | None) -> dict[tuple[str, str], dict]:
    if not doc:
        return {}
    return {(c["algorithm_id"], c["suite_id"]): c for c in doc.get("cells", [])
            if c.get("metrics") and c.get("status") == "evaluated"}


async def run_local_tests(
    repo_root: str,
    *,
    algorithms: list[str] | None = None,
    suites: list[str] | None = None,
    tags: list[str] | None = None,
    overrides: dict[str, Any] | None = None,
    baseline: dict | None = None,
    jobs: int = 1,
    work_dir: str | None = None,
    rel_tolerance: float | None = None,
    on_cell=None,
) -> dict[str, Any]:
    repo_root = os.path.abspath(repo_root)
    manifest = load_manifest(repo_root)
    name = manifest.name or os.path.basename(repo_root.rstrip(os.sep))
    engine = RunnerEngine(work_dir or default_work_dir(), local_projects={name: repo_root})
    rev = await Workspace.local_revision(repo_root)
    cells = manifest.cells(algorithms, suites, tags)
    base_idx = _baseline_index(baseline)
    sem = asyncio.Semaphore(max(1, jobs))
    started = time.time()

    async def one(algo_id: str, suite_id: str) -> dict[str, Any]:
        async with sem:
            algo = manifest.algorithm(algo_id)
            ov = {k: v for k, v in (overrides or {}).items() if k in algo.spec().by_key()}
            req = RunRequest(run_id=new_id(), project_id=name, kind="adhoc",
                             repo=RepoRef(kind="local", name=name), algorithm_id=algo_id,
                             suite_id=suite_id, param_values=ov)
            outcome = await engine.run(req)
            res = outcome.result
            ok = res.status == RunStatus.evaluated
            base = base_idx.get((algo_id, suite_id))
            verdict, gates, deltas = judge(algo, ok, res.metrics, base.get("metrics") if base else None,
                                           rel_tolerance=rel_tolerance)
            cell = {
                "algorithm_id": algo_id, "suite_id": suite_id, "run_id": res.run_id,
                "status": "evaluated" if ok else "failed", "verdict": verdict.value,
                "metrics": res.metrics, "params": res.param_values, "gates": gates,
                "deltas": deltas, "error": res.error, "error_kind": res.error_kind,
                "duration_s": res.duration_s, "run_dir": outcome.run_dir,
                "baseline_sha": (baseline or {}).get("commit_sha") if base else None,
            }
            if on_cell is not None:
                on_cell(cell)
            return cell

    results = await asyncio.gather(*(one(a, s) for a, s in cells))
    summary: dict[str, int] = {v.value: 0 for v in Verdict if v is not Verdict.pending}
    for c in results:
        summary[c["verdict"]] = summary.get(c["verdict"], 0) + 1
    failing = sum(summary.get(v.value, 0) for v in FAILING_VERDICTS)
    return {
        "algops_test": REPORT_VERSION,
        "project": name,
        "repo_root": repo_root,
        "commit_sha": rev.sha,
        "dirty": rev.dirty,
        "started_at": started,
        "duration_s": round(time.time() - started, 3),
        "cells": results,
        "summary": summary,
        "ok": failing == 0 and bool(results),
    }


def load_report(path: str) -> dict:
    with open(path, encoding="utf-8") as fh:
        doc = json.load(fh)
    if "cells" not in doc:
        raise ValueError(f"{path} is not an `algops test --json` report")
    return doc
