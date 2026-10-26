"""Schedules: cron benchmarks and git-watch (new commit => benchmark)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from algops.core import ScheduleCreate, ScheduleOut
from algops.tracking import get_session, repo
from algops.tracking.models import Schedule

from ..services.scheduler import next_cron, valid_cron
from .deps import require_api

router = APIRouter(prefix="/api/v1/schedules", tags=["schedules"])


def _out(sc: Schedule) -> ScheduleOut:
    return ScheduleOut(
        schedule_id=sc.schedule_id, name=sc.name, kind=sc.kind, cron=sc.cron,
        interval_s=sc.interval_s, scope=sc.scope, enabled=sc.enabled,
        last_fired_at=repo.iso(sc.last_fired_at), next_fire_at=repo.iso(sc.next_fire_at),
        last_benchmark_id=sc.last_benchmark_id,
    )


@router.post("", response_model=ScheduleOut, dependencies=[Depends(require_api)])
def create_schedule(body: ScheduleCreate) -> ScheduleOut:
    if body.kind == "cron" and (not body.cron or not valid_cron(body.cron)):
        raise HTTPException(400, f"invalid cron expression {body.cron!r}")
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
    with get_session() as s:
        row = repo.create_schedule(
            s, name=body.name, kind=body.kind, cron=body.cron if body.kind == "cron" else None,
            interval_s=body.interval_s, scope=scope, enabled=body.enabled,
        )
        if row.kind == "cron" and row.cron:
            row.next_fire_at = next_cron(row.cron, repo.utcnow())
        return _out(row)


@router.get("", response_model=list[ScheduleOut])
def list_schedules() -> list[ScheduleOut]:
    with get_session() as s:
        return [_out(sc) for sc in repo.list_schedules(s)]


@router.patch("/{schedule_id}", response_model=ScheduleOut, dependencies=[Depends(require_api)])
def toggle(schedule_id: str, enabled: bool) -> ScheduleOut:
    with get_session() as s:
        row = repo.get_schedule(s, schedule_id)
        if row is None:
            raise HTTPException(404, "schedule not found")
        row.enabled = enabled
        return _out(row)


@router.delete("/{schedule_id}", dependencies=[Depends(require_api)])
def delete(schedule_id: str) -> dict:
    with get_session() as s:
        if not repo.delete_schedule(s, schedule_id):
            raise HTTPException(404, "schedule not found")
    return {"deleted": schedule_id}
