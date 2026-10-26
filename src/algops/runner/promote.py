"""Promotion: propose a campaign's best parameters back to the repository as a git commit.

Git stays the source of truth. ALGOps never edits a working tree in place: it creates a
throw-away worktree at the campaign's pinned base sha on a new branch, rewrites only the
``default:`` of the changed parameters (comments and formatting preserved), commits, and returns
the patch. Optionally the branch is pushed so the master can open a PR / MR.
"""

from __future__ import annotations

import io
import json
import os
import re
import shutil
import tempfile
from typing import Any

import yaml
from ruamel.yaml import YAML

from algops.core import PromoteRequest, PromoteResult, load_manifest
from algops.core.manifest import find_manifest

from algops.core.schemas import check_git_ref

from .workspace import SHA_RE, GitError, Workspace, git


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


def _typed(new: Any, old: Any, dtype: Any = None) -> Any:
    """The value as the parameter's declared ``dtype`` (falls back to the old default's type).

    The dtype wins over the YAML scalar: a float parameter written as ``default: 1`` must
    receive the tuned ``1.5``, not a rounded ``2``.
    """
    dtype = str(dtype or "").lower()
    if dtype == "float":
        return float(new)
    if dtype == "int":
        return int(round(float(new)))
    if dtype == "bool":
        if isinstance(new, str):
            return new.strip().lower() in ("1", "true", "yes", "on")
        return bool(new)
    if dtype == "enum":
        return new
    if isinstance(old, bool):
        return bool(new)
    if isinstance(old, int) and not isinstance(old, bool):
        return int(round(float(new))) if float(new).is_integer() else float(new)
    if isinstance(old, float):
        return float(new)
    return new


def _type_ok(value: Any, dtype: Any) -> bool:
    dtype = str(dtype or "").lower()
    if dtype == "float":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if dtype == "int":
        return isinstance(value, int) and not isinstance(value, bool)
    if dtype == "bool":
        return isinstance(value, bool)
    return True


def _yaml_scalar(value: Any) -> str:
    """Render a scalar so PyYAML (YAML 1.1) reads back the same type."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        text = f"{value:.12g}"
        if "e" in text or "E" in text:
            text = f"{value:.6e}"
        elif "." not in text and "inf" not in text and "nan" not in text:
            text += ".0"
        return text
    text = str(value)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.\-]*", text) and text.lower() not in (
            "true", "false", "yes", "no", "on", "off", "null", "none", "~"):
        return text
    return json.dumps(text, ensure_ascii=False)


def _token_end(line: str, col: int) -> int:
    if col < len(line) and line[col] in "\"'":
        q = line[col]
        i = col + 1
        while i < len(line):
            if line[i] == q and (q == "'" or line[i - 1] != "\\"):
                if q == "'" and i + 1 < len(line) and line[i + 1] == "'":
                    i += 2
                    continue
                return i + 1
            i += 1
        return len(line)
    i = col
    while i < len(line) and line[i] not in ",}]#\r\n":
        i += 1
    while i > col and line[i - 1] in " \t":
        i -= 1
    return i


def _splice(line: str, col: int, new_text: str) -> str:
    end = _token_end(line, col)
    old_len = end - col
    rest = line[end:]
    m = re.match(r"^( +)(#.*)$", rest.rstrip("\r\n"))
    if m:  # keep a trailing comment in its column when possible
        pad = max(1, len(m.group(1)) - (len(new_text) - old_len))
        nl = rest[len(rest.rstrip("\r\n")):]
        rest = " " * pad + m.group(2) + nl
    return line[:col] + new_text + rest


def rewrite_defaults(path: str, algorithm_id: str, inline: bool,
                     values: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Rewrite ``default:`` for ``values`` in the YAML file at ``path``; returns the changes.

    Surgical: only the default scalars' text changes (comments, flow-style spacing, key order
    stay byte-identical) so the proposed commit is a minimal, reviewable diff. Falls back to a
    ruamel round-trip dump if the surgical edit cannot be verified.
    """
    y = _yaml()
    with open(path, encoding="utf-8") as fh:
        original = fh.read()
    doc = y.load(original)
    items = _param_items(doc, algorithm_id, inline)
    by_key = {str(item.get("key")): item for item in items if hasattr(item, "get")}
    missing = [k for k in values if k not in by_key]
    if missing:
        raise KeyError(f"parameters not declared in {os.path.basename(path)}: {missing}")
    changed: dict[str, dict[str, Any]] = {}
    edits: list[tuple[int, int, str]] = []
    for key, new in values.items():
        item = by_key[key]
        old = item.get("default")
        if _same(old, new):
            continue
        typed = _typed(new, old, item.get("dtype"))
        if _same(old, typed) and type(old) is type(typed):
            continue
        changed[key] = {"old": old, "new": typed}
        try:
            line, col = item.lc.value("default")
            edits.append((line, col, _yaml_scalar(typed)))
        except Exception:  # noqa: BLE001 — no position info: use the fallback dump
            edits = []
            break
    if not changed:
        return changed
    text = None
    if len(edits) == len(changed):
        lines = original.splitlines(keepends=True)
        for line, col, new_text in sorted(edits, reverse=True):
            lines[line] = _splice(lines[line], col, new_text)
        candidate = "".join(lines)
        try:
            check = _param_items(yaml.safe_load(candidate), algorithm_id, inline)
            got = {str(i.get("key")): i.get("default") for i in check}
            if all(_same(got.get(k), c["new"]) for k, c in changed.items()):
                text = candidate
        except Exception:  # noqa: BLE001
            text = None
    if text is None:
        for key, c in changed.items():
            by_key[key]["default"] = c["new"]
        buf = io.StringIO()
        y.dump(doc, buf)
        text = buf.getvalue()
    _verify(text, algorithm_id, inline, changed, {k: by_key[k].get("dtype") for k in changed})
    with open(path, "w", encoding="utf-8", newline="") as fh:
        fh.write(text)
    return changed


def _verify(text: str, algorithm_id: str, inline: bool, changed: dict[str, dict[str, Any]],
            dtypes: dict[str, Any]) -> None:
    """Re-read the rewritten YAML as a plain loader would and check every proposed default."""
    got = {str(i.get("key")): i.get("default")
           for i in _param_items(yaml.safe_load(text), algorithm_id, inline) if hasattr(i, "get")}
    bad = [k for k, c in changed.items()
           if not _same(got.get(k), c["new"]) or not _type_ok(got.get(k), dtypes.get(k))]
    if bad:
        raise ValueError(f"rewritten defaults do not read back correctly for {bad} — refusing to commit")


async def promote(req: PromoteRequest, *, workspace: Workspace, local_path: str | None,
                  author_name: str, author_email: str, push_remote: str = "origin") -> PromoteResult:
    base = req.repo.sha
    if not base or base.startswith("local") or "-dirty" in base:
        return PromoteResult(ok=False, base_sha=base,
                             error="promotion needs a clean committed base sha (campaign ran on "
                                   "uncommitted/local code — commit it and re-run the campaign)")
    if not SHA_RE.match(base):
        return PromoteResult(ok=False, base_sha=base, error=f"invalid base sha {base!r}")
    try:
        check_git_ref(req.branch)
    except ValueError as exc:
        return PromoteResult(ok=False, base_sha=base, error=str(exc))
    if req.repo.kind == "git":
        if not req.repo.url:
            return PromoteResult(ok=False, error="git project without url")
        mirror = await workspace.ensure_mirror(req.repo.url, fetch=False)
        git_dir_args = ["--git-dir", mirror]
        wt_lock = workspace.worktree_lock(mirror)
    else:
        if not local_path:
            return PromoteResult(ok=False, error=f"local project {req.repo.name!r} not served here")
        git_dir_args = ["-C", local_path]
        wt_lock = workspace.worktree_lock(local_path)

    tmp = tempfile.mkdtemp(prefix="algops_promote_")
    wt = os.path.join(tmp, "wt")
    env = {
        "GIT_AUTHOR_NAME": author_name, "GIT_AUTHOR_EMAIL": author_email,
        "GIT_COMMITTER_NAME": author_name, "GIT_COMMITTER_EMAIL": author_email,
    }
    try:
        try:
            async with wt_lock:
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
        except (KeyError, ValueError) as exc:
            return PromoteResult(ok=False, base_sha=base, error=str(exc).strip("'\""))
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
            async with wt_lock:
                await git(*git_dir_args, "worktree", "remove", "--force", wt, timeout=120)
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(tmp, ignore_errors=True)
