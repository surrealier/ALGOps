"""Per-checkout environment setup (``env.setup`` in algops.yaml), executed at most once.

A marker keyed by (checkout path + identity, commit sha, setup commands) is written under the
runner's work dir — never inside the user's repository — so a git worktree is set up once per
commit and a local project is re-set-up whenever its HEAD or its setup commands change. The
checkout identity (inode/mtime of its ``.git`` entry) changes when a pruned worktree is
re-created at the same path, so a fresh checkout never inherits a stale "already set up".
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import time
from dataclasses import dataclass

from algops.core import EnvSpec

from .process import EnvPolicy, run_process


def checkout_identity(path: str) -> str:
    """Changes whenever the checkout at ``path`` is deleted and re-created."""
    dot_git = os.path.join(path, ".git")
    try:
        st = os.stat(dot_git)
        if os.path.isfile(dot_git):  # a worktree: the file is written once, at `worktree add`
            return f"wt:{st.st_ino}:{st.st_mtime_ns}"
        return f"repo:{st.st_ino}"  # a repository's .git dir (its mtime churns on every git op)
    except OSError:
        try:
            return f"dir:{os.stat(path).st_ino}"
        except OSError:
            return "missing"


@dataclass
class SetupOutcome:
    ok: bool
    log: str = ""
    cached: bool = False
    error: str | None = None


class EnvSetup:
    def __init__(self, marker_root: str, env_policy: EnvPolicy | None = None) -> None:
        self.root = os.path.join(os.path.abspath(marker_root), "setup")
        os.makedirs(self.root, exist_ok=True)
        self._locks: dict[str, asyncio.Lock] = {}
        self.env_policy = env_policy or EnvPolicy()

    def _key(self, path: str, sha: str, env: EnvSpec) -> str:
        blob = json.dumps({"path": os.path.abspath(path), "identity": checkout_identity(path),
                           "sha": sha, "setup": env.setup, "vars": env.vars, "python": env.python},
                          sort_keys=True)
        return hashlib.sha256(blob.encode()).hexdigest()[:24]

    async def ensure(self, path: str, sha: str, env: EnvSpec) -> SetupOutcome:
        if not env.setup:
            return SetupOutcome(ok=True, cached=True)
        key = self._key(path, sha, env)
        marker = os.path.join(self.root, key + ".ok")
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            if os.path.isfile(marker):
                return SetupOutcome(ok=True, cached=True)
            proc_env = self.env_policy.environ()
            proc_env.update(env.vars)
            proc_env["ALGOPS_REPO_ROOT"] = os.path.abspath(path)
            logs: list[str] = []
            deadline = time.monotonic() + env.setup_timeout_s
            for cmd in env.setup:
                remaining = max(1.0, deadline - time.monotonic())
                res = await run_process(cmd, cwd=path, env=proc_env, timeout=remaining, shell=True)
                logs.append(f"$ {cmd}\n{res.output}")
                if not res.ok:
                    why = "timed out" if res.timed_out else f"exit {res.returncode}"
                    log = "\n".join(logs)
                    log_path = os.path.join(self.root, key + ".failed.log")
                    with open(log_path, "w", encoding="utf-8") as fh:
                        fh.write(log)
                    tail = "\n".join(res.output.strip().splitlines()[-25:])
                    return SetupOutcome(ok=False, log=log, error=(
                        f"env.setup `{cmd}` failed ({why}) — full log: {log_path}\n{tail}").strip())
            log = "\n".join(logs)
            with open(marker, "w", encoding="utf-8") as fh:
                fh.write(log[-100_000:])
            return SetupOutcome(ok=True, log=log)
