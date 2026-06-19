"""Project CRUD."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from sqlalchemy.orm import Session

from algops.core import ProjectCreate, ProjectOut
from algops.tracking import get_session, repo
from algops.tracking.models import Project

router = APIRouter(prefix="/api/v1/projects", tags=["projects"])


def _to_out(s: Session, p: Project) -> ProjectOut:
    return ProjectOut(
        project_id=p.project_id,
        name=p.name,
        modality=p.modality,
        description=p.description,
        slave_online=bool(repo.online_slave_for_project(s, p.project_id)),
    )


@router.post("", response_model=ProjectOut)
def create_project(body: ProjectCreate) -> ProjectOut:
    with get_session() as s:
        return _to_out(s, repo.create_project(s, body))


@router.get("", response_model=list[ProjectOut])
def list_projects() -> list[ProjectOut]:
    with get_session() as s:
        return [_to_out(s, p) for p in repo.list_projects(s)]


@router.get("/{project_id}", response_model=ProjectOut)
def get_project(project_id: str) -> ProjectOut:
    with get_session() as s:
        p = repo.get_project(s, project_id)
        if p is None:
            raise HTTPException(404, "project not found")
        return _to_out(s, p)
