"""Repository workspaces on a runner host: git mirrors + per-commit worktrees, or local paths.

* **git projects** — one bare ``--mirror`` clone per project (cheap incremental fetches) and one
  detached worktree per commit sha. Every run executes in the worktree of the exact commit the
  master pinned, so results are reproducible and concurrent runs of different commits never
  interfere. Credentials come from the host's normal git setup (ssh keys / credential helper).
* **local projects** — a path on this host, executed in place (dev fast-loop, legacy slaves).
  The "sha" is ``HEAD`` (suffixed ``-dirty`` with uncommitted changes) when the path is a git
  repository root, else ``local``.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import time
from dataclasses import dataclass

from .process import run_process

SHA_RE = re.compile(r"^[0-9a-f]{7,64}$")


class GitError(RuntimeError):
    pass


async def git(*args: str, cwd: str | None = None, timeout: float = 600.0,
              env: dict[str, str] | None = None) -> str:
    full_env = dict(os.environ)
    full_env.setdefault("GIT_TERMINAL_PROMPT", "0")  # never hang on a credential prompt
    full_env.update(env or {})
    res = await run_process(["git", *args], cwd=cwd, env=full_env, timeout=timeout)
    if not res.ok:
        why = "timed out" if res.timed_out else f"exit {res.returncode}"
        raise GitError(f"git {' '.join(args[:3])} … failed ({why}): {res.output.strip()[-2000:]}")
    return res.output.strip()


_FP_SKIP = {".git", ".hg", ".svn", ".venv", "venv", "node_modules", "__pycache__", "build", "dist",
            "target", "out", ".algops", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".idea", ".vscode"}


def _fingerprint(root: str, limit: int = 50_000) -> str:
    """Cheap content fingerprint of a non-git directory: (path, size, mtime) of its files."""
    h = hashlib.sha1()
    count = 0
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d not in _FP_SKIP and not d.startswith("."))
        for name in sorted(files):
            try:
                st = os.stat(os.path.join(dirpath, name))
            except OSError:
                continue
            rel = os.path.relpath(os.path.join(dirpath, name), root)
            h.update(f"{rel}|{st.st_size}|{st.st_mtime_ns}\n".encode())
            count += 1
            if count >= limit:
                return h.hexdigest()[:12]
    return h.hexdigest()[:12]


def _slug(text: str) -> str:
    base = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[-40:] or "repo"
    return f"{base}-{hashlib.sha1(text.encode()).hexdigest()[:8]}"


@dataclass
class Checkout:
    path: str
    sha: str
    dirty: bool = False


class Workspace:
    def __init__(self, root: str, keep_checkouts: int = 20) -> None:
        self.root = os.path.abspath(root)
        self.keep = max(1, keep_checkouts)
        self._locks: dict[str, asyncio.Lock] = {}
        self._in_use: dict[str, int] = {}
        self._fetched_at: dict[str, float] = {}
        os.makedirs(self.root, exist_ok=True)

    # ------------------------------------------------------------------ helpers

    def _lock(self, key: str) -> asyncio.Lock:
        return self._locks.setdefault(key, asyncio.Lock())

    def mirror_path(self, url: str) -> str:
        return os.path.join(self.root, "mirrors", _slug(url) + ".git")

    def checkout_path(self, url: str, sha: str) -> str:
        return os.path.join(self.root, "checkouts", _slug(url), sha)

    # ------------------------------------------------------------------ git projects

    async def ensure_mirror(self, url: str, *, fetch: bool = True, max_age_s: float = 0.0) -> str:
        mirror = self.mirror_path(url)
        async with self._lock("mirror:" + mirror):
            if not os.path.isdir(mirror):
                os.makedirs(os.path.dirname(mirror), exist_ok=True)
                tmp = mirror + ".partial"
                shutil.rmtree(tmp, ignore_errors=True)
                await git("clone", "--mirror", "--quiet", url, tmp, timeout=1800)
                os.replace(tmp, mirror)
                self._fetched_at[mirror] = time.monotonic()
            elif fetch and time.monotonic() - self._fetched_at.get(mirror, 0.0) >= max_age_s:
                await git("--git-dir", mirror, "remote", "update", "--prune", timeout=1800)
                self._fetched_at[mirror] = time.monotonic()
        return mirror

    async def resolve(self, url: str, ref: str | None) -> str:
        """Fetch, then resolve ``ref`` (branch / tag / sha; None => remote default) to a sha."""
        mirror = await self.ensure_mirror(url, fetch=True)
        target = ref or "HEAD"
        try:
            return await git("--git-dir", mirror, "rev-parse", "--verify", "--quiet",
                             f"{target}^{{commit}}")
        except GitError:
            raise GitError(f"cannot resolve ref {target!r} in {url}") from None

    async def _has_commit(self, mirror: str, sha: str) -> bool:
        try:
            await git("--git-dir", mirror, "cat-file", "-e", f"{sha}^{{commit}}", timeout=60)
            return True
        except GitError:
            return False

    async def checkout(self, url: str, sha: str) -> Checkout:
        """A detached worktree at ``sha`` (created once, reused by every run of that commit)."""
        if not SHA_RE.match(sha):
            raise GitError(f"refusing to check out non-sha {sha!r} (resolve refs first)")
        mirror = await self.ensure_mirror(url, fetch=False)
        path = self.checkout_path(url, sha)
        async with self._lock("co:" + path):
            if os.path.isfile(os.path.join(path, ".git")):
                os.utime(path)
                return Checkout(path=path, sha=sha)
            if not await self._has_commit(mirror, sha):
                await self.ensure_mirror(url, fetch=True)
                if not await self._has_commit(mirror, sha):
                    raise GitError(f"commit {sha} not found in {url}")
            shutil.rmtree(path, ignore_errors=True)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            await git("--git-dir", mirror, "worktree", "prune", timeout=120)
            await git("--git-dir", mirror, "worktree", "add", "--detach", "--force", path, sha,
                      timeout=1800)
        await self._prune(url)
        return Checkout(path=path, sha=sha)

    def acquire(self, path: str) -> None:
        self._in_use[path] = self._in_use.get(path, 0) + 1

    def release(self, path: str) -> None:
        n = self._in_use.get(path, 1) - 1
        if n <= 0:
            self._in_use.pop(path, None)
        else:
            self._in_use[path] = n

    async def _prune(self, url: str) -> None:
        base = os.path.dirname(self.checkout_path(url, "0" * 40))
        if not os.path.isdir(base):
            return
        entries = [os.path.join(base, d) for d in os.listdir(base)]
        entries = [e for e in entries if os.path.isdir(e)]
        if len(entries) <= self.keep:
            return
        entries.sort(key=lambda p: os.path.getmtime(p))
        mirror = self.mirror_path(url)
        for path in entries[: len(entries) - self.keep]:
            if self._in_use.get(path):
                continue
            try:
                await git("--git-dir", mirror, "worktree", "remove", "--force", path, timeout=120)
            except GitError:
                shutil.rmtree(path, ignore_errors=True)

    # ------------------------------------------------------------------ local projects

    @staticmethod
    async def local_revision(path: str) -> Checkout:
        """HEAD (+ ``-dirty-<diffhash>``) for a git repository root, else ``local-<fingerprint>``.

        The suffixes change whenever the code changes, so per-revision caches (env.setup, the
        master's revision table) never serve stale results for edited local code.
        """
        path = os.path.abspath(path)
        try:
            top = await git("-C", path, "rev-parse", "--show-toplevel", timeout=30)
        except (GitError, FileNotFoundError):
            top = None
        if top is None or os.path.realpath(top) != os.path.realpath(path):
            return Checkout(path=path, sha="local-" + _fingerprint(path), dirty=True)
        try:
            sha = await git("-C", path, "rev-parse", "HEAD", timeout=30)
        except GitError:  # a repository without commits yet
            return Checkout(path=path, sha="local-" + _fingerprint(path), dirty=True)
        diff = await git("-C", path, "diff", "HEAD", "--binary", timeout=120)
        # Untracked (not ignored) files count too — a new source file must invalidate caches.
        # Build outputs belong in .gitignore; well-known output dirs are skipped regardless.
        others = await git("-C", path, "ls-files", "--others", "--exclude-standard", timeout=120)
        untracked = []
        for rel in sorted(x for x in others.splitlines() if x.strip()):
            if any(part in _FP_SKIP for part in rel.split("/")[:-1]):
                continue
            try:
                st = os.stat(os.path.join(path, rel))
            except OSError:
                continue
            untracked.append(f"{rel}|{st.st_size}|{st.st_mtime_ns}")
        if not diff.strip() and not untracked:
            return Checkout(path=path, sha=sha)
        h = hashlib.sha1(diff.encode("utf-8", "replace"))
        h.update("\n".join(untracked).encode())
        digest = h.hexdigest()[:8]
        return Checkout(path=path, sha=f"{sha}-dirty-{digest}", dirty=True)
