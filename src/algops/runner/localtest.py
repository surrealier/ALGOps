"""``algops test`` — run every algorithm × suite of one repository locally and judge the results.

No master, no network: the same engine and verdict rules as a master benchmark, so a repo's own
CI can gate merges (``algops test --baseline main-results.json``) and developers can reproduce a
failing cell exactly.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import time
from typing import Any

from algops.core import (
    FAILING_VERDICTS,
    RepoRef,
    RunRequest,
    RunResult,
    RunStatus,
    Verdict,
    load_manifest,
    new_id,
)
from algops.extraction.verdict import judge

from .engine import RunnerEngine
from .protocol import RunOutcome
from .workspace import Workspace

REPORT_VERSION = 1


def default_work_dir() -> str:
    base = os.environ.get("XDG_CACHE_HOME") or os.path.join(os.path.expanduser("~"), ".cache")
    return os.path.join(base, "algops", "work")


def prune_run_dirs(work_dir: str, keep: int = 200) -> None:
    """Keep the CLI cache bounded: only the newest ``keep`` run directories survive."""
    runs = os.path.join(work_dir, "runs")
    if not os.path.isdir(runs):
        return
    entries = [os.path.join(runs, d) for d in os.listdir(runs)]
    entries = [e for e in entries if os.path.isdir(e)]
    if len(entries) <= keep:
        return
    entries.sort(key=os.path.getmtime)
    for path in entries[: len(entries) - keep]:
        shutil.rmtree(path, ignore_errors=True)


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
    timeout_s: float | None = None,
) -> dict[str, Any]:
    repo_root = os.path.abspath(repo_root)
    manifest = load_manifest(repo_root)
    name = manifest.name or os.path.basename(repo_root.rstrip(os.sep))
    work = work_dir or default_work_dir()
    prune_run_dirs(work)
    engine = RunnerEngine(work, local_projects={name: repo_root})
    rev = await Workspace.local_revision(repo_root)
    cells = manifest.cells(algorithms, suites, tags)
    selected = {a for a, _ in cells} or {a.id for a in manifest.algorithms}
    declared = {k for a in manifest.algorithms if a.id in selected for k in a.spec().by_key()}
    unknown = sorted(k for k in (overrides or {}) if k not in declared)
    if unknown:
        raise ValueError(f"--set: unknown parameter(s) {unknown} for algorithm(s) {sorted(selected)}")
    base_idx = _baseline_index(baseline)
    sem = asyncio.Semaphore(max(1, jobs))
    started = time.time()

    async def one(algo_id: str, suite_id: str) -> dict[str, Any]:
        async with sem:
            algo = manifest.algorithm(algo_id)
            ov = {k: v for k, v in (overrides or {}).items() if k in algo.spec().by_key()}
            req = RunRequest(run_id=new_id(), project_id=name, kind="adhoc",
                             repo=RepoRef(kind="local", name=name), algorithm_id=algo_id,
                             suite_id=suite_id, param_values=ov, timeout_s=timeout_s)
            try:
                outcome = await engine.run(req)
            except Exception as exc:  # noqa: BLE001 — one broken cell never hangs or sinks the run
                outcome = RunOutcome(result=RunResult(run_id=req.run_id, status=RunStatus.failed,
                                                      error=f"{type(exc).__name__}: {exc}", error_kind="infra"))
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
                "has_baseline": base is not None,
                "log_tail": "\n".join(outcome.log.strip().splitlines()[-30:]) if not ok else None,
            }
            if on_cell is not None:
                on_cell(cell)
            return cell

    raw = await asyncio.gather(*(one(a, s) for a, s in cells), return_exceptions=True)
    results: list[dict[str, Any]] = []
    for (a, su), r in zip(cells, raw):
        if isinstance(r, BaseException):
            r = {"algorithm_id": a, "suite_id": su, "run_id": None, "status": "failed",
                 "verdict": Verdict.error.value, "metrics": None, "params": None, "gates": [],
                 "deltas": {}, "error": f"{type(r).__name__}: {r}", "error_kind": "infra",
                 "duration_s": None, "run_dir": None, "baseline_sha": None, "has_baseline": False,
                 "log_tail": None}
            if on_cell is not None:
                on_cell(r)
        results.append(r)
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
    try:
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
    except json.JSONDecodeError as exc:
        raise ValueError(f"baseline report {path} is not valid JSON: {exc}") from exc
    if not isinstance(doc, dict) or "cells" not in doc:
        raise ValueError(f"{path} is not an `algops test --json` report")
    return doc
