"""Project sync: resolve a ref to a commit on some runner and snapshot the manifest at it."""

from __future__ import annotations

from algops.core import ManifestError, RepoManifest, RevisionOut, manifest_from_wire
from algops.tracking import get_session, repo
from algops.tracking.models import ProjectRevision

from ..context import MasterContext


def revision_out(rev: ProjectRevision) -> RevisionOut:
    return RevisionOut(
        revision_id=rev.revision_id, project_id=rev.project_id, ref=rev.ref, sha=rev.sha,
        dirty=bool(rev.dirty), synced_at=repo.iso(rev.synced_at) or "", manifest=rev.manifest,
    )


def record_revision(project_id: str, *, ref: str | None, sha: str, dirty: bool, manifest: dict) -> RevisionOut:
    """Upsert by (project, sha): an immutable commit keeps one revision row."""
    with get_session() as s:
        existing = repo.latest_revision(s, project_id, sha=sha)
        if existing is not None and not dirty and not existing.dirty and existing.manifest == manifest:
            existing.synced_at = repo.utcnow()
            existing.ref = ref
            rev = existing
        else:
            rev = repo.add_revision(s, project_id, ref=ref, sha=sha, dirty=dirty, manifest=manifest)
        return revision_out(rev)


async def sync_project(ctx: MasterContext, project_id: str, ref: str | None = None) -> tuple[RevisionOut | None, str | None]:
    """Ask a runner to resolve ``ref`` (None => project default) and read the manifest there."""
    assert ctx.dispatcher is not None
    res = await ctx.dispatcher.sync(project_id, ref)
    if not res.ok or not res.sha or res.manifest is None:
        return None, res.error or "sync failed"
    try:
        manifest_from_wire(res.manifest)
    except Exception as exc:  # noqa: BLE001
        return None, f"invalid manifest at {res.sha[:12]}: {exc}"
    rev = record_revision(project_id, ref=ref, sha=res.sha, dirty=res.dirty, manifest=res.manifest)
    return rev, None


def latest_manifest(project_id: str, sha: str | None = None) -> tuple[RevisionOut | None, RepoManifest | None]:
    with get_session() as s:
        rev = repo.latest_revision(s, project_id, sha=sha)
        if rev is None:
            return None, None
        out = revision_out(rev)
    try:
        return out, manifest_from_wire(out.manifest)
    except (ManifestError, ValueError):
        return out, None
