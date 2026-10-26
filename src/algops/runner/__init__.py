"""algops.runner — language-agnostic execution of algorithm runs (contract v2).

``protocol``   request.json -> program -> result.json, metrics derivation, artifact collection
``workspace``  git mirrors + per-commit worktrees, local projects
``envsetup``   once-per-commit ``env.setup`` with markers outside the repo
``promote``    best params -> branch commit + patch (+ push)
``engine``     the per-host facade used by the slave and the ``algops`` CLI
"""

from __future__ import annotations

from .engine import RunnerEngine, bundled_toy_path
from .protocol import RunOutcome, execute_run, resolve_suite_path
from .workspace import GitError, Workspace

__all__ = [
    "RunnerEngine",
    "bundled_toy_path",
    "RunOutcome",
    "execute_run",
    "resolve_suite_path",
    "GitError",
    "Workspace",
]
