"""``algops init`` — scaffold a contract-v2 repository (manifest, params, entrypoint, sample suite)."""

from __future__ import annotations

import os
import re
import shutil
import stat

_BUNDLE = os.path.join(os.path.dirname(__file__), "bundle")
_V2 = os.path.join(_BUNDLE, "templates", "v2")


def _tmpl(name: str) -> str:
    with open(os.path.join(_V2, name), encoding="utf-8") as fh:
        return fh.read()


def _write(path: str, text: str, force: bool, written: list[str], skipped: list[str]) -> None:
    if os.path.exists(path) and not force:
        skipped.append(path)
        return
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    written.append(path)


def init_repo(root: str, *, algorithm: str = "main", lang: str = "python", name: str | None = None,
              force: bool = False, with_docs: bool = True) -> tuple[list[str], list[str]]:
    """Write the v2 scaffold into ``root``. Returns (written, skipped-existing)."""
    if not re.match(r"^[a-z][a-z0-9_-]{0,63}$", algorithm):
        raise ValueError(f"invalid algorithm id {algorithm!r}")
    if lang not in ("python", "command"):
        raise ValueError("lang must be 'python' or 'command'")
    root = os.path.abspath(root)
    name = name or os.path.basename(root.rstrip(os.sep)) or "project"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", name)
    algo_key = re.sub(r"[^a-z0-9_]", "_", algorithm)
    fmt = {"name": name, "algo": algorithm, "algo_key": algo_key, "algo_env": algo_key.upper()}
    if lang == "python":
        entry = f'      python: "algops/{algorithm}/run.py:run"'
    else:
        entry = f'      command: ["sh", "algops/{algorithm}/run.sh"]'
    written: list[str] = []
    skipped: list[str] = []
    _write(os.path.join(root, "algops.yaml"), _tmpl("algops.yaml.tmpl").format(entrypoint=entry, **fmt),
           force, written, skipped)
    _write(os.path.join(root, "algops", algorithm, "params.yaml"), _tmpl("params.yaml.tmpl").format(**fmt),
           force, written, skipped)
    if lang == "python":
        _write(os.path.join(root, "algops", algorithm, "run.py"), _tmpl("run.py.tmpl").format(**fmt),
               force, written, skipped)
    else:
        path = os.path.join(root, "algops", algorithm, "run.sh")
        _write(path, _tmpl("run.sh.tmpl").format(**fmt), force, written, skipped)
        if path in written:
            os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    _write(os.path.join(root, "data", "smoke", "items.csv"), _tmpl("items.csv.tmpl"), force, written, skipped)
    if with_docs:
        for doc in ("AGENTS.md", "CLAUDE.md", "params.schema.json"):
            dst = os.path.join(root, doc) if doc != "params.schema.json" else os.path.join(root, "algops", doc)
            if os.path.exists(dst) and not force:
                skipped.append(dst)
                continue
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copyfile(os.path.join(_BUNDLE, doc), dst)
            written.append(dst)
    return written, skipped
