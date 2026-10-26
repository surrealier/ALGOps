"""Benchmarks: run every algorithm × suite of every (or selected) project and judge each cell."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from algops.core import BenchmarkCreate, BenchmarkOut
from algops.tracking import get_session, repo

from ..context import MasterContext
from ..services.benchmarks import benchmark_out, start_benchmark
from .deps import get_ctx, require_api

router = APIRouter(prefix="/api/v1/benchmarks", tags=["benchmarks"])


@router.post("", response_model=BenchmarkOut, dependencies=[Depends(require_api)])
async def create_benchmark(body: BenchmarkCreate, ctx: MasterContext = Depends(get_ctx)) -> BenchmarkOut:
    scope = None
    if body.scope:
        scope = []
        with get_session() as s:
            for sc in body.scope:
                proj = repo.find_project(s, sc.project_id)
                if proj is None:
                    raise HTTPException(404, f"project {sc.project_id!r} not found")
                item = sc.model_dump(mode="json", exclude_none=True)
                item["project_id"] = proj.project_id
                scope.append(item)
    if body.baseline not in ("previous", "none"):
        with get_session() as s:
            if repo.get_benchmark(s, body.baseline) is None:
                raise HTTPException(404, f"baseline benchmark {body.baseline!r} not found")
    bid = start_benchmark(ctx, name=body.name, scope=scope, baseline=body.baseline, trigger=body.trigger)
    with get_session() as s:
        return benchmark_out(repo.get_benchmark(s, bid))


@router.get("", response_model=list[BenchmarkOut])
def list_benchmarks(limit: int = 50, project_id: str | None = None) -> list[BenchmarkOut]:
    with get_session() as s:
        return [benchmark_out(b) for b in repo.list_benchmarks(s, limit, project_id)]


@router.get("/{benchmark_id}", response_model=BenchmarkOut)
def get_benchmark(benchmark_id: str) -> BenchmarkOut:
    with get_session() as s:
        b = repo.get_benchmark(s, benchmark_id)
        if b is None:
            raise HTTPException(404, "benchmark not found")
        return benchmark_out(b, with_cells=True)


@router.post("/{benchmark_id}/accept", dependencies=[Depends(require_api)])
def accept(benchmark_id: str, cell_id: str | None = None) -> dict:
    """Accept cells (all, or one) as the new baseline — e.g. an intentional trade-off."""
    with get_session() as s:
        b = repo.get_benchmark(s, benchmark_id)
        if b is None:
            raise HTTPException(404, "benchmark not found")
        n = 0
        for c in repo.list_cells(s, benchmark_id):
            if (cell_id is None or c.cell_id == cell_id) and c.metrics:
                c.accepted = True
                n += 1
    return {"benchmark_id": benchmark_id, "accepted": n}
