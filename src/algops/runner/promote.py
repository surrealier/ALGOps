"""Promotion: propose a campaign's best parameters back to the repository as a git commit.

Git stays the source of truth. ALGOps never edits a working tree in place: it creates a
throw-away worktree at the campaign's pinned base sha on a new branch, rewrites only the
``default:`` of the changed parameters (comments and formatting preserved), commits, and returns
the patch. Optionally the branch is pushed so the master can open a PR / MR.
"""

from __future__ import annotations

import io
import os
import shutil
import tempfile
from typing import Any

from ruamel.yaml import YAML

from algops.core import PromoteRequest, PromoteResult, load_manifest
from algops.core.manifest import find_manifest

from .workspace import GitError, Workspace, git


def _yaml() -> YAML:
    y = YAML()
    y.preserve_quotes = True
    y.width = 4096
    y.indent(mapping=2, sequence=4, offset=2)
    return y


def _param_items(doc: Any, algorithm_id: str, inline: bool) -> list:
    """The list of param mappings to edit: params.yaml `params:` or inline in algops.yaml."""
    if not inline:
        if isinstance(doc, list):
            return doc
        return list(doc.get("params") or [])
    for algo in doc.get("algorithms") or []:
        if algo.get("id") == algorithm_id:
            return list(algo.get("params") or [])
    raise KeyError(f"algorithm {algorithm_id!r} not found in algops.yaml")


def _same(a: Any, b: Any) -> bool:
    if isinstance(a, bool) or isinstance(b, bool):
        return a is b or a == b
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return a == b


def _typed(new: Any, old: Any) -> Any:
    """Keep the YAML scalar type of the old default (int stays int, float stays float)."""
    if isinstance(old, bool):
        return bool(new)
    if isinstance(old, int) and not isinstance(old, bool):
        return int(round(float(new)))
    if isinstance(old, float):
        return float(new)
    return new


def rewrite_defaults(path: str, algorithm_id: str, inline: bool,
                     values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Rewrite ``default:`` for ``values`` in the YAML file at ``path``; returns the changes."""
    y = _yaml()
    with open(path, encoding="utf-8") as fh:
        doc = y.load(fh)
    items = _param_items(doc, algorithm_id, inline)
    by_key = {str(item.get("key")): item for item in items if hasattr(item, "get")}
    missing = [k for k in values if k not in by_key]
    if missing:
        raise KeyError(f"parameters not declared in {os.path.basename(path)}: {missing}")
    changed: dict[str, dict[str, Any]] = {}
    for key, new in values.items():
        item = by_key[key]
        old = item.get("default")
        if _same(old, new):
            continue
        item["default"] = _typed(new, old)
        changed[key] = {"old": old, "new": item["default"]}
    if changed:
        buf = io.StringIO()
        y.dump(doc, buf)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(buf.getvalue())
    return changed


async def promote(req: PromoteRequest, *, workspace: Workspace, local_path: str | None,
                  author_name: str, author_email: str, push_remote: str = "origin") -> PromoteResult:
    base = req.repo.sha
    if not base or base == "local" or base.endswith("-dirty"):
        return PromoteResult(ok=False, base_sha=base,
                             error="promotion needs a clean committed base sha (campaign ran on "
                                   "uncommitted/local code — commit it and re-run the campaign)")
    if req.repo.kind == "git":
        if not req.repo.url:
            return PromoteResult(ok=False, error="git project without url")
        git_dir_args = ["--git-dir", await workspace.ensure_mirror(req.repo.url, fetch=False)]
    else:
        if not local_path:
            return PromoteResult(ok=False, error=f"local project {req.repo.name!r} not served here")
        git_dir_args = ["-C", local_path]

    tmp = tempfile.mkdtemp(prefix="algops_promote_")
    wt = os.path.join(tmp, "wt")
    env = {
        "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email,
    }
    try:
        try:
            await git(*git_dir_args, "worktree", "add", "-B", req.branch, wt, base, timeout=600)
        except GitError as exc:
            return PromoteResult(ok=False, base_sha=base, error=str(exc))
        manifest = load_manifest(wt)
        algo = manifest.algorithm(req.algorithm_id)
        if isinstance(algo.params, str):
            target, inline = os.path.join(wt, algo.params), False
        elif isinstance(algo.params, list):
            mpath = find_manifest(wt)
            if mpath is None:
                return PromoteResult(ok=False, base_sha=base, error="inline params without algops.yaml")
            target, inline = mpath, True
        else:
            return PromoteResult(ok=False, base_sha=base, error=f"{algo.id} declares no params")
        try:
            changed = rewrite_defaults(target, algo.id, inline, req.params)
        except KeyError as exc:
            return PromoteResult(ok=False, base_sha=base, error=str(exc))
        if not changed:
            return PromoteResult(ok=False, base_sha=base, error="best parameters equal the current defaults — nothing to propose")
        # Re-validate the edited spec before committing (never propose a broken params file).
        load_manifest(wt).algorithm(algo.id).spec().effective(req.params)
        rel = os.path.relpath(target, wt).replace(os.sep, "/")
        await git("-C", wt, "add", rel, env=env)
        await git("-C", wt, "commit", "--quiet", "-m", req.message, env=env)
        sha = await git("-C", wt, "rev-parse", "HEAD")
        patch = await git("-C", wt, "format-patch", "-1", "--stdout", "HEAD")
        pushed, push_error = False, None
        if req.push:
            try:
                dest = req.repo.url if req.repo.kind == "git" else push_remote
                await git("-C", wt, "push", "--quiet", dest or push_remote,
                          f"refs/heads/{req.branch}:refs/heads/{req.branch}", timeout=600)
                pushed = True
            except GitError as exc:
                push_error = str(exc)
        return PromoteResult(ok=True, branch=req.branch, commit_sha=sha, base_sha=base,
                             changed=changed, files=[rel], patch=patch + "\n", pushed=pushed,
                             error=push_error)
    except Exception as exc:  # noqa: BLE001
        return PromoteResult(ok=False, base_sha=base, error=f"{type(exc).__name__}: {exc}")
    finally:
        try:
            await git(*git_dir_args, "worktree", "remove", "--force", wt, timeout=120)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)
