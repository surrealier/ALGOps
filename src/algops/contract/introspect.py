"""Heuristic project introspection (``algops introspect``, AGENTS.md step 0).

A coding agent does the real understanding; this scanner seeds it by listing candidate hardcoded
constants — numeric literals in assignments, defaults, ``#define``s and comparisons — across
Python (AST) and C/C++/CUDA, JS/TS, Go, Rust, Java/Kotlin, C#, MATLAB, shell (regex). Output is a
starter ``algops/UNDERSTANDING.md``; an existing one is never overwritten (candidates go to
``algops/UNDERSTANDING.candidates.md`` instead).
"""

from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass

_SKIP_DIRS = {".venv", "venv", "__pycache__", ".git", "node_modules", "algops", "build", "dist",
              "target", "out", ".algops", "vendor", "third_party", ".idea", ".vscode"}
_TRIVIAL = {0, 1, -1, 0.0, 1.0, 100, 255, 2}
_REGEX_EXT = {".c", ".h", ".cc", ".cpp", ".cxx", ".hpp", ".hh", ".cu", ".cuh", ".js", ".mjs", ".cjs",
              ".ts", ".tsx", ".jsx", ".go", ".rs", ".java", ".kt", ".cs", ".m", ".swift", ".scala",
              ".rb", ".lua", ".sh"}
_NUM = r"-?(?:\d+\.\d*|\.\d+|\d+)(?:[eE][-+]?\d+)?[fFlLuU]?"
_PATTERNS = [
    ("define", re.compile(rf"^\s*#\s*define\s+([A-Za-z_]\w*)\s+\(?({_NUM})\)?\s*(?://.*|/\*.*)?$")),
    ("assign", re.compile(rf"\b([A-Za-z_]\w*)\s*(?::=|=)(?!=)\s*({_NUM})(?=\s*(?:[;,)}}\]]|$|//|#|%))")),
    ("compare", re.compile(rf"(?:[<>]=?)\s*({_NUM})\b")),
]


@dataclass
class Candidate:
    file: str
    line: int
    name: str
    value: float


def _num(text: str) -> float | None:
    try:
        return float(text.rstrip("fFlLuU"))
    except ValueError:
        return None


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


def _scan_text(rel: str, text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for i, raw in enumerate(text.splitlines(), start=1):
        line = raw.strip()
        if not line or line.startswith(("//", "/*", "*", "--")) or (line.startswith("#") and not line.startswith("#define") and not line.startswith("# define")):
            continue
        seen: set[tuple[int, int]] = set()  # (start, end) spans already reported on this line
        for kind, rx in _PATTERNS:
            if kind == "assign" and any(c.line == i and c.name.startswith("#define") for c in out[-3:]):
                continue
            for m in rx.finditer(raw):
                group = 1 if kind == "compare" else 2
                span = m.span(group)
                if span in seen:
                    continue
                if kind == "compare":
                    name, val = "<compare>", _num(m.group(1))
                else:
                    name, val = m.group(1), _num(m.group(2))
                if val is None or val in _TRIVIAL:
                    continue
                seen.add(span)
                out.append(Candidate(rel, i, name if kind != "define" else f"#define {name}", val))
    return out


def scan_candidates(project_root: str) -> list[Candidate]:
    out: list[Candidate] = []
    for root, dirs, files in os.walk(project_root):
        dirs[:] = sorted(d for d in dirs if d not in _SKIP_DIRS and not d.startswith("."))
        for fname in sorted(files):
            fpath = os.path.join(root, fname)
            rel = os.path.relpath(fpath, project_root)
            ext = os.path.splitext(fname)[1].lower()
            try:
                if os.path.getsize(fpath) > 2_000_000:
                    continue
                with open(fpath, encoding="utf-8", errors="replace") as fh:
                    text = fh.read()
            except OSError:
                continue
            if ext == ".py":
                try:
                    visitor = _Visitor(rel)
                    visitor.visit(ast.parse(text))
                    out.extend(visitor.found)
                except SyntaxError:
                    out.extend(_scan_text(rel, text))
            elif ext in _REGEX_EXT:
                out.extend(_scan_text(rel, text))
    return out


def write_understanding(project_root: str) -> str:
    """Write the starter (or a candidates file next to an existing one) and return its path."""
    candidates = scan_candidates(project_root)
    algops_dir = os.path.join(project_root, "algops")
    os.makedirs(algops_dir, exist_ok=True)
    path = os.path.join(algops_dir, "UNDERSTANDING.md")
    table = [
        "| file:line | 이름 | 현재값 | (추정) 타입/범위/영향 |",
        "| --- | --- | --- | --- |",
        *[f"| {c.file}:{c.line} | `{c.name}` | {c.value:g} | TODO |" for c in candidates[:300]],
    ]
    if not candidates:
        table.append("| (후보 없음) | | | |")
    if os.path.exists(path):  # never clobber the agent's / human's notes
        path = os.path.join(algops_dir, "UNDERSTANDING.candidates.md")
        lines = ["# 외부화 후보 (자동 스캔 — 기존 UNDERSTANDING.md는 보존)", "", *table]
    else:
        lines = [
            "# 프로젝트 이해 (ALGOps Phase 0 — 자동 초안, 에이전트가 보강)",
            "",
            "## 알고리즘 목록",
            "- TODO: 이 저장소에 있는 알고리즘(독립적으로 평가할 단위)과 각자의 입력·출력·정답.",
            "",
            "## 테스트셋 (suites)",
            "- TODO: 각 테스트셋의 위치(저장소 상대경로 또는 ${DATASETS}/...)와 용도(smoke/full …).",
            "",
            "## 무엇이 좋은가 (지표와 방향)",
            "- TODO: 예) fp_rate ↓, recall ↑ (≥ 0.85 유지), latency_ms ↓",
            "",
            "## 파이프라인 단계 (stage id)",
            "- TODO: ai_inference → postproc_* → decision 순서로 단계 정의.",
            "",
            "## 외부화 후보 파라미터 (자동 스캔)",
            *table,
        ]
    lines += [
        "",
        f"> 자동 스캔으로 {len(candidates)}개의 숫자 리터럴 후보를 찾았습니다. 실제 후처리/결정 파라미터만 골라",
        "> `algops/<algorithm>/params.yaml`(또는 algops.yaml 인라인)로 외부화하세요 — 범위/step/영향 태그 포함.",
    ]
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    return path
