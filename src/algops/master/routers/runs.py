"""Run listing for an experiment (the per-iteration evaluations)."""

from __future__ import annotations

from fastapi import APIRouter

from algops.core import Metrics, RunOut
from algops.tracking import get_session, repo

router = APIRouter(prefix="/api/v1", tags=["runs"])


@router.get("/experiments/{experiment_id}/runs", response_model=list[RunOut])
def list_runs(experiment_id: str) -> list[RunOut]:
    with get_session() as s:
        out = []
        for r in repo.list_runs(s, experiment_id):
            out.append(
                RunOut(
                    run_id=r.run_id,
                    experiment_id=r.experiment_id,
                    iteration_index=r.iteration_index,
                    status=r.status,
                    param_values=r.param_values or {},
                    metrics=Metrics(**r.metrics) if r.metrics else None,
                    score=r.score,
                    summary_md_uri=r.summary_md_uri,
                )
            )
        return out
