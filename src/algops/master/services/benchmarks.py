"""Benchmarks — run every (project × algorithm × suite) cell at pinned commits and judge it.

    sync each project in scope (ref -> sha, manifest)  →  expand cells  →  dispatch runs in
    parallel (bounded by runner capacity)  →  per cell: gates + baseline comparison  →  summary,
    WebSocket progress, deterministic notification.

Benchmark runs always use the parameter defaults *at that commit* — git is the source of truth.
"""

from __future__ import annotations

import asyncio
from typing import Any

from algops.core import (
    FAILING_VERDICTS,
    BenchmarkCellOut,
    BenchmarkOut,
    BenchmarkStatus,
    RepoManifest,
    RunRequest,
    RunStatus,
    Verdict,
    manifest_from_wire,
    new_id,
)
from algops.extraction import summarize_markdown
from algops.extraction.verdict import judge
from algops.notify import notify_benchmark_complete
from algops.tracking import get_session, repo
from algops.tracking.models import Benchmark, BenchmarkCell

from ..context import MasterContext
from ..dispatch import repo_ref
from .projects import sync_project


def cell_out(c: BenchmarkCell, project_name: str = "") -> BenchmarkCellOut:
    return BenchmarkCellOut(
        cell_id=c.cell_id, benchmark_id=c.benchmark_id, project_id=c.project_id,
        project_name=project_name, algorithm_id=c.algorithm_id, suite_id=c.suite_id,
        commit_sha=c.commit_sha, run_id=c.run_id, verdict=c.verdict, metrics=c.metrics,
        gates=c.gates or [], baseline_run_id=c.baseline_run_id, baseline_sha=c.baseline_sha,
        deltas=c.deltas or {}, error=c.error, duration_s=c.duration_s,
    )


def benchmark_out(b: Benchmark, *, with_cells: bool = False) -> BenchmarkOut:
    cells = None
    if with_cells:
        with get_session() as s:
            names = {p.project_id: p.name for p in repo.list_projects(s)}
            cells = [cell_out(c, names.get(c.project_id, "")) for c in repo.list_cells(s, b.benchmark_id)]
    return BenchmarkOut(
        benchmark_id=b.benchmark_id, name=b.name, status=b.status, trigger=b.trigger,
        created_at=repo.iso(b.created_at) or "", finished_at=repo.iso(b.finished_at),
        scope=b.scope, summary=b.summary or {}, revisions=b.revisions or {}, cells=cells,
    )


def _delta_detail(cell: dict[str, Any]) -> str:
    if cell.get("error"):
        return str(cell["error"]).splitlines()[0][:160]
    parts = [f"{g['metric']} {g['op']} {g['value']} (실제 {g['actual']})" for g in cell.get("gates") or []
             if not g["passed"]]
    for m, d in (cell.get("deltas") or {}).items():
        if d.get("status") == "regressed":
            parts.append(f"{m} {d['baseline']:.4g}→{d['current']:.4g}")
    return "; ".join(parts)


async def _run_cell(ctx: MasterContext, *, benchmark_id: str, baseline: str, cell_id: str,
                    project_id: str, project_name: str, sha: str, manifest: RepoManifest,
                    algorithm_id: str, suite_id: str) -> dict[str, Any]:
    assert ctx.dispatcher is not None
    algo = manifest.algorithm(algorithm_id)
    run_id = new_id()
    with get_session() as s:
        proj = repo.get_project(s, project_id)
        ref = repo_ref(proj, sha=sha) if proj else None
        repo.create_run(s, run_id=run_id, experiment_id=None, project_id=project_id,
                        iteration_index=0, param_values={}, benchmark_id=benchmark_id,
                        algorithm_id=algorithm_id, suite_id=suite_id, commit_sha=sha, kind="benchmark")
        cell = repo.get_cell(s, cell_id)
        if cell is not None:
            cell.run_id = run_id
    await ctx.ws.publish(benchmark_id, {"type": "cell_started", "benchmark_id": benchmark_id,
                                        "cell_id": cell_id, "run_id": run_id})
    req = RunRequest(run_id=run_id, project_id=project_id, benchmark_id=benchmark_id, repo=ref,
                     algorithm_id=algorithm_id, suite_id=suite_id, kind="benchmark",
                     timeout_s=algo.timeout_s)
    timeout = algo.timeout_s + manifest.env.setup_timeout_s + 120.0
    res, slave_id = await ctx.dispatcher.run(req, runs_on=algo.runs_on, timeout=timeout)
    ok = res.status == RunStatus.evaluated
    metrics = res.metrics or {}
    with get_session() as s:
        repo.record_run_result(s, run_id=run_id, status=res.status.value, metrics=metrics,
                               score=None, n_items=res.n_items, csv_manifest_uri=res.csv_manifest_uri,
                               result=res, slave_id=slave_id, log_uri=res.log_uri)
        base = repo.baseline_cell(s, project_id=project_id, algorithm_id=algorithm_id,
                                  suite_id=suite_id, exclude_benchmark_id=benchmark_id, baseline=baseline)
        base_metrics = dict(base.metrics) if base and base.metrics else None
        base_run, base_sha = (base.run_id, base.commit_sha) if base else (None, None)
    verdict, gates, deltas = judge(algo, ok, metrics, base_metrics,
                                   rel_tolerance=ctx.settings.benchmark_rel_tolerance)
    if ok:
        obj = algo.default_objective()
        md = summarize_markdown(
            project_name=project_name, iteration_index=0, param_values=res.param_values or {},
            metrics=metrics, target_metric=obj.metric if obj else next(iter(metrics), ""),
            target_value=obj.target if obj else None,
            direction=obj.direction if obj else "maximize", objective=obj,
            n_items=res.n_items, algorithm_id=algorithm_id, suite_id=suite_id, commit_sha=sha,
        )
        uri = ctx.store.put_text(f"runs/{run_id}/summary.md", md)
        with get_session() as s:
            run = repo.get_run(s, run_id)
            if run is not None:
                run.summary_md_uri = uri
    error = None if ok else f"[{res.error_kind}] {res.error or 'failed'}"
    with get_session() as s:
        cell = repo.get_cell(s, cell_id)
        if cell is not None:
            cell.verdict = verdict.value
            cell.metrics = metrics or None
            cell.gates = gates
            cell.deltas = deltas
            cell.baseline_run_id = base_run
            cell.baseline_sha = base_sha
            cell.error = error
            cell.duration_s = res.duration_s
            cell.commit_sha = res.commit_sha or sha
            out = cell_out(cell, project_name)
    await ctx.ws.publish(benchmark_id, {"type": "cell_done", "benchmark_id": benchmark_id,
                                        **out.model_dump(mode="json")})
    return {"verdict": verdict.value, "project": project_name, "algorithm_id": algorithm_id,
            "suite_id": suite_id, "error": error, "gates": gates, "deltas": deltas}


async def run_benchmark(ctx: MasterContext, benchmark_id: str) -> str:
    """Execute a created benchmark to completion. Returns the final status."""
    assert ctx.dispatcher is not None
    with get_session() as s:
        b = repo.get_benchmark(s, benchmark_id)
        if b is None:
            return "missing"
        b.status = BenchmarkStatus.running.value
        scope = list(b.scope or [])
        baseline = b.baseline or "previous"
        name = b.name
        if not scope:
            scope = [{"project_id": p.project_id} for p in repo.list_projects(s)]
    await ctx.ws.publish(benchmark_id, {"type": "benchmark_started", "benchmark_id": benchmark_id})

    plans: list[dict[str, Any]] = []
    revisions: dict[str, str] = {}
    results: list[dict[str, Any]] = []
    for sc in scope:
        pid = sc["project_id"]
        with get_session() as s:
            proj = repo.get_project(s, pid)
            pname = proj.name if proj else pid
        if proj is None:
            continue
        rev, err = await sync_project(ctx, pid, sc.get("ref"))
        if rev is None:
            with get_session() as s:
                c = repo.add_cell(s, benchmark_id=benchmark_id, project_id=pid, algorithm_id="*",
                                  suite_id="*", commit_sha=None)
                c.verdict = Verdict.error.value
                c.error = f"sync failed: {err}"
            results.append({"verdict": Verdict.error.value, "project": pname, "algorithm_id": "*",
                            "suite_id": "*", "error": f"sync failed: {err}"})
            continue
        manifest = manifest_from_wire(rev.manifest)
        revisions[pid] = rev.sha
        for algo_id, suite_id in manifest.cells(sc.get("algorithms"), sc.get("suites"), sc.get("tags")):
            with get_session() as s:
                c = repo.add_cell(s, benchmark_id=benchmark_id, project_id=pid, algorithm_id=algo_id,
                                  suite_id=suite_id, commit_sha=rev.sha)
                cid = c.cell_id
            plans.append({"cell_id": cid, "project_id": pid, "project_name": pname, "sha": rev.sha,
                          "manifest": manifest, "algorithm_id": algo_id, "suite_id": suite_id})
    with get_session() as s:
        b = repo.get_benchmark(s, benchmark_id)
        if b is not None:
            b.revisions = revisions
    await ctx.ws.publish(benchmark_id, {"type": "benchmark_planned", "benchmark_id": benchmark_id,
                                        "cells": len(plans), "revisions": revisions})

    parallel = ctx.settings.benchmark_max_parallel or ctx.dispatcher.capacity(list(revisions)) or 1
    sem = asyncio.Semaphore(max(1, parallel))

    async def guarded(plan: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            try:
                return await _run_cell(ctx, benchmark_id=benchmark_id, baseline=baseline, **plan)
            except Exception as exc:  # noqa: BLE001 — one broken cell never sinks the benchmark
                with get_session() as s:
                    c = repo.get_cell(s, plan["cell_id"])
                    if c is not None:
                        c.verdict = Verdict.error.value
                        c.error = f"{type(exc).__name__}: {exc}"
                return {"verdict": Verdict.error.value, "project": plan["project_name"],
                        "algorithm_id": plan["algorithm_id"], "suite_id": plan["suite_id"],
                        "error": str(exc)}

    results += await asyncio.gather(*(guarded(p) for p in plans))

    summary: dict[str, int] = {v.value: 0 for v in Verdict if v is not Verdict.pending}
    for r in results:
        summary[r["verdict"]] = summary.get(r["verdict"], 0) + 1
    failing = [dict(r, detail=_delta_detail(r)) for r in results
               if Verdict(r["verdict"]) in FAILING_VERDICTS]
    if not results:
        status = BenchmarkStatus.error
    elif failing:
        status = BenchmarkStatus.failed
    else:
        status = BenchmarkStatus.passed
    with get_session() as s:
        b = repo.get_benchmark(s, benchmark_id)
        if b is not None:
            b.status = status.value
            b.summary = summary
            b.finished_at = repo.utcnow()
            if not results:
                b.error = "no cells (no projects in scope, or every sync failed)"
    try:
        notify_benchmark_complete(ctx.settings, dedup_key=f"benchmark:{benchmark_id}", name=name,
                                  status=status.value, summary=summary, failing=failing)
    except Exception:  # noqa: BLE001
        pass
    await ctx.ws.publish(benchmark_id, {"type": "benchmark_done", "benchmark_id": benchmark_id,
                                        "status": status.value, "summary": summary})
    return status.value


def start_benchmark(ctx: MasterContext, *, name: str, scope: list[dict] | None, baseline: str,
                    trigger: str, schedule_id: str | None = None) -> str:
    """Create the benchmark row and launch it (in-process task or Celery)."""
    with get_session() as s:
        b = repo.create_benchmark(s, name=name, trigger=trigger, scope=scope, baseline=baseline,
                                  schedule_id=schedule_id)
        bid = b.benchmark_id
    if ctx.settings.dev_mode:
        ctx.spawn(run_benchmark(ctx, bid))
    else:  # pragma: no cover - requires Celery/Redis
        from ..celery_app import run_benchmark_task

        run_benchmark_task.delay(bid)
    return bid
