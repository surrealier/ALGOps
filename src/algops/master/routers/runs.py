"""Runs: the evaluations of a campaign, and single-run lookup."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

from algops.core import RunOut
from algops.tracking import get_session, repo
from algops.tracking.models import Run

router = APIRouter(prefix="/api/v1", tags=["runs"])


def run_out(r: Run) -> RunOut:
    return RunOut(
        run_id=r.run_id,
        experiment_id=r.experiment_id,
        iteration_index=r.iteration_index,
        status=r.status,
        param_values=r.param_values or {},
        metrics=r.metrics or None,
        score=r.score,
        summary_md_uri=r.summary_md_uri,
        benchmark_id=r.benchmark_id,
        algorithm_id=r.algorithm_id,
        suite_id=r.suite_id,
        commit_sha=r.commit_sha,
        kind=r.kind,
        duration_s=r.duration_s,
        error=r.error,
        log_uri=r.log_uri,
        attempt=r.attempt or 0,
    )


@router.get("/experiments/{experiment_id}/runs", response_model=list[RunOut])
def list_runs(experiment_id: str, attempt: int | None = None, all_attempts: bool = False) -> list[RunOut]:
    """Runs of the latest attempt by default (``all_attempts=true`` for the full history)."""
    with get_session() as s:
        exp = repo.get_experiment(s, experiment_id)
        if exp is None:
            raise HTTPException(404, "experiment not found")
        if attempt is None and not all_attempts:
            attempt = exp.attempt or 0
        return [run_out(r) for r in repo.list_runs(s, experiment_id, attempt)]


@router.get("/runs/{run_id}", response_model=RunOut)
def get_run(run_id: str) -> RunOut:
    with get_session() as s:
        r = repo.get_run(s, run_id)
        if r is None:
            raise HTTPException(404, "run not found")
        return run_out(r)
