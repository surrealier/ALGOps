"""CLI entry points the coding agent (or user) invokes during externalization."""

from __future__ import annotations

import os
import shutil
import sys

from .introspect import write_understanding
from .verify import run_gate

_BUNDLE = os.path.join(os.path.dirname(__file__), "bundle")


def materialize_bundle(target_root: str) -> list[str]:
    """Drop the contract files (AGENTS.md/CLAUDE.md/params.schema.json + templates) into a repo."""
    os.makedirs(target_root, exist_ok=True)
    written: list[str] = []
    for name in ("AGENTS.md", "CLAUDE.md", "params.schema.json"):
        dst = os.path.join(target_root, name)
        shutil.copyfile(os.path.join(_BUNDLE, name), dst)
        written.append(dst)
    tdir = os.path.join(target_root, "algops", "_templates")
    os.makedirs(tdir, exist_ok=True)
    for name in os.listdir(os.path.join(_BUNDLE, "templates")):
        dst = os.path.join(tdir, name)
        shutil.copyfile(os.path.join(_BUNDLE, "templates", name), dst)
        written.append(dst)
    return written


def cmd_init_contract() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    files = materialize_bundle(root)
    print("ALGOps 계약 번들 배치 완료:")
    for f in files:
        print(" -", f)
    print("\n다음: 코딩 에이전트에게 AGENTS.md/CLAUDE.md를 따르도록 지시하세요.")


def cmd_introspect() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    print("작성:", write_understanding(root))


def cmd_verify() -> None:
    root = sys.argv[1] if len(sys.argv) > 1 else "."
    result = run_gate(root, algops_dir="algops", strict="--strict" in sys.argv)
    print(result.render())
    sys.exit(0 if result.ok else 1)
