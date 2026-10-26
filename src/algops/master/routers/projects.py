"""Projects (repositories): register git repos, sync revisions, inspect manifests and history."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from algops.core import (
    ProjectCreate,
    ProjectOut,
    ProjectUpdate,
    RevisionOut,
    RunOut,
    manifest_from_wire,
)
from algops.tracking import get_session, repo
from algops.tracking.models import Project

from ..context import MasterContext
from ..services.projects import latest_manifest, revision_out, sync_project
from .deps import get_ctx, require_api
from .runs import run_out

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _to_out(s: Session, p: Project) -> ProjectOut:
    rev = repo.latest_revision(s, p.project_id)
    algos: list[str] = []
    suites: list[str] = []
    if rev is not None:
        try:
            m = manifest_from_wire(rev.manifest)
            algos = [a.id for a in m.algorithms]
            suites = [x.id for x in m.suites]
        except Exception:  # noqa: BLE001
            pass
    return ProjectOut(
        project_id=p.project_id,
        name=p.name,
        modality=p.modality,
        description=p.description,
        slave_online=bool(repo.eligible_slaves(s, p)),
        repo_kind=p.repo_kind,
        repo_url=p.repo_url,
        default_ref=p.default_ref,
        watch=bool(p.watch),
        latest_sha=rev.sha if rev else None,
        synced_at=repo.iso(rev.synced_at) if rev else None,
        algorithms=algos,
        suites=suites,
    )


def _get(s: Session, ident: str) -> Project:
    p = repo.find_project(s, ident)
    if p is None:
        raise HTTPException(404, "project not found")
    return p


@router.post("", response_model=ProjectOut, dependencies=[Depends(require_api)])
def create_project(body: ProjectCreate) -> ProjectOut:
    with get_session() as s:
        if repo.get_project_by_name(s, body.name) is not None:
            raise HTTPException(409, f"project {body.name!r} already exists")
        return _to_out(s, repo.create_project(s, body))


@router.get("", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    with get_session() as s:
        return [_to_out(s, p) for p in repo.list_projects(s)]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str) -> ProjectOut:
    with get_session() as s:
        return _to_out(s, _get(s, project_id))


@router.patch("/{project_id}", response_model=ProjectOut, dependencies=[Depends(require_api)])
def update_project(project_id: str, body: ProjectUpdate) -> ProjectOut:
    with get_session() as s:
        p = _get(s, project_id)
        for field in body.model_fields_set:
            value = getattr(body, field)
            if field == "repo_url" and p.repo_kind != "git":
                raise HTTPException(400, "repo_url can only be changed on git projects")
            setattr(p, field, value)
        return _to_out(s, p)


@router.post("/{project_id}/sync", response_model=RevisionOut, dependencies=[Depends(require_api)])
async def sync(project_id: str, ref: str | None = None, ctx: MasterContext = Depends(get_ctx)) -> RevisionOut:
    with get_session() as s:
        pid = _get(s, project_id).project_id
    rev, err = await sync_project(ctx, pid, ref)
    if rev is None:
        raise HTTPException(502, f"sync failed: {err}")
    return rev


@router.get("/{project_id}/revisions", response_model=list[RevisionOut])
def revisions(project_id: str, limit: int = 20) -> list[RevisionOut]:
    with get_session() as s:
        p = _get(s, project_id)
        return [revision_out(r) for r in repo.list_revisions(s, p.project_id, limit)]


@router.get("/{project_id}/manifest")
def manifest(project_id: str, sha: str | None = None) -> dict:
    with get_session() as s:
        pid = _get(s, project_id).project_id
    rev, m = latest_manifest(pid, sha)
    if rev is None:
        raise HTTPException(404, "project has not been synced yet")
    out = {"sha": rev.sha, "ref": rev.ref, "dirty": rev.dirty, "synced_at": rev.synced_at,
           "manifest": rev.manifest, "valid": m is not None}
    if m is not None:
        out["algorithms"] = [
            {"id": a.id, "description": a.description, "params": len(a.spec().params),
             "suites": [x.id for x in m.suites_for(a.id)],
             "metrics": {k: v.direction.value for k, v in a.metric_specs().items()},
             "objective": a.default_objective().model_dump(mode="json") if a.default_objective() else None,
             "gates": [g.model_dump(mode="json") for g in a.gates], "runs_on": a.runs_on}
            for a in m.algorithms
        ]
    return out


@router.get("/{project_id}/history", response_model=list[RunOut])
def history(project_id: str, algorithm_id: str, suite_id: str, limit: int = 50) -> list[RunOut]:
    """Benchmark runs of one cell over time (newest first) — the trend behind regressions."""
    with get_session() as s:
        pid = _get(s, project_id).project_id
        return [run_out(r) for r in repo.list_cell_history(s, pid, algorithm_id, suite_id, limit)]
