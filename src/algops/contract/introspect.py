"""Heuristic project introspection (plan §2B Phase 0).

A coding agent does the real understanding; this scanner seeds it by listing candidate
hardcoded constants (numeric literals in assignments, default args, comparisons) that likely
belong in ``params.yaml``. Output is a starter ``algops/UNDERSTANDING.md`` the agent refines.
"""

from __future__ import annotations

import ast
import os
from dataclasses import dataclass

_SKIP_DIRS = {".venv", "__pycache__", ".git", "node_modules", "algops", "build", "dist"}
_TRIVIAL = {0, 1, -1, 0.0, 1.0, 100, 255, 2}


@dataclass
class Candidate:
    file: str
    line: int
    name: str
    value: float


class _Visitor(ast.NodeVisitor):
    def __init__(self, rel: str) -> None:
        self.rel = rel
        self.found: list[Candidate] = []

    def _add(self, name: str, node: ast.AST, value) -> None:
        if isinstance(value, bool) or value in _TRIVIAL:
            return
        self.found.append(Candidate(self.rel, getattr(node, "lineno", 0), name, float(value)))

    def visit_Assign(self, node: ast.Assign) -> None:
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, (int, float)):
            target = node.targets[0]
            name = target.id if isinstance(target, ast.Name) else "<assign>"
            self._add(name, node, node.value.value)
        self.generic_visit(node)

    def visit_Compare(self, node: ast.Compare) -> None:
        for comp in node.comparators:
            if isinstance(comp, ast.Constant) and isinstance(comp.value, (int, float)):
                self._add("<compare>", comp, comp.value)
        self.generic_visit(node)

    def visit_arguments(self, node: ast.arguments) -> None:
        for default in node.defaults:
            if isinstance(default, ast.Constant) and isinstance(default.value, (int, float)):
                self._add("<default>", default, default.value)
        self.generic_visit(node)


def scan_candidates(project_root: str) -> list[Candidate]:
    out: list[Candidate] = []
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if d not in _SKIP_DIRS]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            try:
                tree = ast.parse(open(fpath, encoding="utf-8").read())
            except Exception:
                continue
            visitor = _Visitor(os.path.relpath(fpath, project_root))
            visitor.visit(tree)
            out.extend(visitor.found)
    return out


def write_understanding(project_root: str) -> str:
    """Write a starter algops/UNDERSTANDING.md and return its path."""
    candidates = scan_candidates(project_root)
    algops_dir = os.path.join(project_root, "algops")
    os.makedirs(algops_dir, exist_ok=True)
    path = os.path.join(algops_dir, "UNDERSTANDING.md")
    lines = [
        "# 프로젝트 이해 (ALGOps Phase 0 — 자동 초안, 에이전트가 보강)",
        "",
        "## 목적 / 후처리 목표",
        "- TODO: 이 프로젝트가 무엇을 하는지, 후처리의 목표(예: 오탐 완화)는 무엇인지.",
        "",
        "## 파이프라인 단계 (stage id — CSV 규약과 일치시킬 것)",
        "- TODO: ai_inference → postproc_* → decision 순서로 단계 정의.",
        "",
        "## 외부화 후보 파라미터 (자동 스캔)",
        "| file:line | 이름 | 현재값 | (추정) 타입/범위/영향 |",
        "| --- | --- | --- | --- |",
    ]
    for c in candidates[:200]:
        lines.append(f"| {c.file}:{c.line} | `{c.name}` | {c.value} | TODO |")
    if not candidates:
        lines.append("| (후보 없음) | | | |")
    lines += [
        "",
        f"> 자동 스캔으로 {len(candidates)}개의 숫자 리터럴 후보를 찾았습니다. ",
        "> 실제 후처리 파라미터만 골라 `algops/params.yaml`로 외부화하세요(범위/step/영향 태그 포함).",
    ]
    open(path, "w", encoding="utf-8").write("\n".join(lines) + "\n")
    return path
