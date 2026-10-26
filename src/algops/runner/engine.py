"""The runner engine: everything a runner host does for a command, independent of transport.

``sync``    resolve a ref to a sha and read the manifest at that commit
``run``     check out the pinned sha, set up its environment once, execute one run
``promote`` write proposed parameter defaults on a branch (+ push)

The slave poller (distributed mode) and the ``algops`` CLI (local mode) both drive this class.
"""

from __future__ import annotations

import os
import sys

from algops.core import (
    LocalProjectDecl,
    ManifestError,
    PromoteRequest,
    PromoteResult,
    RepoManifest,
    RepoRef,
    RunRequest,
    RunResult,
    RunStatus,
    SyncRequest,
    SyncResult,
    load_manifest,
    manifest_to_wire,
)

from .envsetup import EnvSetup
from .promote import promote as _promote
from .protocol import RunOutcome, execute_run
from .workspace import GitError, Workspace


def bundled_toy_path() -> str:
    import algops.toy_project as toy

    return os.path.dirname(os.path.abspath(toy.__file__))


class RunnerEngine:
    def __init__(
        self,
        work_dir: str,
        *,
        local_projects: dict[str, str] | None = None,
        keep_checkouts: int = 20,
        default_python: str | None = None,
        git_enabled: bool = True,
        author_name: str = "ALGOps",
        author_email: str = "algops@localhost",
        push_remote: str = "origin",
    ) -> None:
        self.work_dir = os.path.abspath(work_dir)
        os.makedirs(self.work_dir, exist_ok=True)
        self.local_projects = {k: os.path.abspath(v) for k, v in (local_projects or {}).items()}
        self.workspace = Workspace(os.path.join(self.work_dir, "repos"), keep_checkouts)
        self.setup = EnvSetup(self.work_dir)
        self.default_python = default_python or sys.executable
        self.git_enabled = git_enabled
        self.author_name = author_name
        self.author_email = author_email
        self.push_remote = push_remote
        self._manifests: dict[str, RepoManifest] = {}  # git checkout path -> manifest (immutable)

    # ------------------------------------------------------------------ manifests

    def _manifest(self, path: str, immutable: bool) -> RepoManifest:
        if immutable and path in self._manifests:
            return self._manifests[path]
        manifest = load_manifest(path)
        if immutable:
            self._manifests[path] = manifest
        return manifest

    async def local_decls(self) -> list[LocalProjectDecl]:
        decls: list[LocalProjectDecl] = []
        for name, path in self.local_projects.items():
            try:
                rev = await Workspace.local_revision(path)
                manifest = self._manifest(path, immutable=False)
                decls.append(LocalProjectDecl(
                    name=name, modality=manifest.algorithms[0].modality, sha=rev.sha,
                    dirty=rev.dirty, manifest=manifest_to_wire(manifest)))
            except Exception as exc:  # noqa: BLE001 — register anyway, surface the error in the UI
                decls.append(LocalProjectDecl(name=name, error=f"{type(exc).__name__}: {exc}"))
        return decls

    def _local_path(self, repo: RepoRef | None) -> str | None:
        if repo is None:
            return next(iter(self.local_projects.values()), None) if len(self.local_projects) == 1 else None
        return self.local_projects.get(repo.name)

    # ------------------------------------------------------------------ commands

    async def sync(self, req: SyncRequest) -> SyncResult:
        repo = req.repo
        try:
            if repo.kind == "local":
                path = self._local_path(repo)
                if path is None:
                    return SyncResult(ok=False, error=f"local project {repo.name!r} is not served by this runner")
                rev = await Workspace.local_revision(path)
                manifest = self._manifest(path, immutable=False)
                return SyncResult(ok=True, sha=rev.sha, ref=repo.ref, dirty=rev.dirty,
                                  manifest=manifest_to_wire(manifest))
            if not self.git_enabled:
                return SyncResult(ok=False, error="git is disabled on this runner")
            if not repo.url:
                return SyncResult(ok=False, error="git project without url")
            sha = await self.workspace.resolve(repo.url, repo.ref)
            co = await self.workspace.checkout(repo.url, sha)
            manifest = self._manifest(co.path, immutable=True)
            return SyncResult(ok=True, sha=sha, ref=repo.ref, manifest=manifest_to_wire(manifest))
        except (GitError, ManifestError, OSError) as exc:
            return SyncResult(ok=False, error=str(exc))
        except Exception as exc:  # noqa: BLE001
            return SyncResult(ok=False, error=f"{type(exc).__name__}: {exc}")

    async def run(self, req: RunRequest) -> RunOutcome:
        repo = req.repo
        path: str | None
        try:
            if repo is None or repo.kind == "local":
                path = self._local_path(repo)
                if path is None:
                    name = repo.name if repo else "?"
                    return self._infra(req, f"local project {name!r} is not served by this runner")
                rev = await Workspace.local_revision(path)
                sha = rev.sha
                manifest = self._manifest(path, immutable=False)
            else:
                if not self.git_enabled:
                    return self._infra(req, "git is disabled on this runner")
                if not repo.url or not repo.sha:
                    return self._infra(req, "git run without url/sha (the master pins every run)")
                co = await self.workspace.checkout(repo.url, repo.sha)
                path, sha = co.path, co.sha
                manifest = self._manifest(path, immutable=True)
        except GitError as exc:
            return self._infra(req, str(exc))
        except ManifestError as exc:
            return self._fail(req, "setup", str(exc))

        self.workspace.acquire(path)
        try:
            setup = await self.setup.ensure(path, sha, manifest.env)
            if not setup.ok:
                out = RunOutcome(result=RunResult(run_id=req.run_id, status=RunStatus.failed,
                                                  error=setup.error, error_kind="setup", commit_sha=sha))
                out.files["_algops/setup.log"] = setup.log.encode("utf-8")
                return out
            return await execute_run(req, repo_root=path, manifest=manifest,
                                     work_dir=self.work_dir, commit_sha=sha,
                                     default_python=self.default_python)
        finally:
            self.workspace.release(path)

    async def promote(self, req: PromoteRequest) -> PromoteResult:
        return await _promote(req, workspace=self.workspace, local_path=self._local_path(req.repo),
                              author_name=self.author_name, author_email=self.author_email,
                              push_remote=self.push_remote)

    # ------------------------------------------------------------------ helpers

    @staticmethod
    def _fail(req: RunRequest, kind: str, error: str) -> RunOutcome:
        return RunOutcome(result=RunResult(run_id=req.run_id, status=RunStatus.failed,
                                           error=error, error_kind=kind))  # type: ignore[arg-type]

    def _infra(self, req: RunRequest, error: str) -> RunOutcome:
        return self._fail(req, "infra", error)
