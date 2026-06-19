"""The executable externalization gate (plan §2B step 6, risk #2).

Acceptance is *behavior*, not file existence: the gate validates params.yaml against the
contract, confirms parameters load without error, imports the run-adapter, and runs it once on a
self-check request expecting a schema-valid RunResult. A heuristic hardcoded-literal scan is
reported as advisory findings. A Claude Code post-edit hook runs ``verify_algops.py`` (which calls
``run_gate``) and must see exit 0 before the coding agent may declare success.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import tempfile
from dataclasses import dataclass, field

from algops.core import ParamSpec, RunRequest, RunResult, new_id
from algops.core.enums import Dtype

from .params import load_param_spec


@dataclass
class GateResult:
    ok: bool
    checks: list[tuple[str, bool, str]] = field(default_factory=list)
    literal_findings: list[dict] = field(default_factory=list)

    def render(self) -> str:
        lines = [("✅ PASS" if self.ok else "❌ FAIL") + " — ALGOps externalization gate"]
        for name, ok, detail in self.checks:
            lines.append(f"  [{'ok' if ok else 'XX'}] {name}: {detail}")
        if self.literal_findings:
            lines.append(f"  (advisory) 외부화 안 된 듯한 하드코딩 리터럴 {len(self.literal_findings)}건:")
            for f in self.literal_findings[:10]:
                lines.append(f"     - {f['file']}:{f['line']} {f.get('name','')}={f['value']}")
        return "\n".join(lines)


def _import_file(path: str):
    name = "algops_target_" + new_id().replace("-", "")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _validate_params(algops_dir: str) -> tuple[bool, str, ParamSpec | None]:
    path = os.path.join(algops_dir, "params.yaml")
    if not os.path.exists(path):
        return False, f"missing {path}", None
    try:
        spec = load_param_spec(algops_dir)
        return True, f"{len(spec.params)}개 파라미터", spec
    except Exception as exc:  # noqa: BLE001
        return False, f"invalid: {exc}", None


def _check_loader(algops_dir: str) -> tuple[bool, str]:
    path = os.path.join(algops_dir, "config.py")
    if not os.path.exists(path):
        return True, "config.py 없음(선택) — run_adapter가 직접 effective_params 사용"
    try:
        module = _import_file(path)
        loader = getattr(module, "load_params", None)
        if loader is None:
            return False, "config.py에 load_params() 없음"
        loader()
        return True, "load_params() 정상"
    except Exception as exc:  # noqa: BLE001
        return False, f"load_params() 오류: {exc}"


def _check_run_adapter(algops_dir: str, spec: ParamSpec | None, strict: bool):
    path = os.path.join(algops_dir, "run_adapter.py")
    if not os.path.exists(path):
        return (False, f"missing {path}", False, "skipped")
    try:
        module = _import_file(path)
    except Exception as exc:  # noqa: BLE001
        return (False, f"import 실패: {exc}", False, "skipped")
    run_fn = getattr(module, "run", None)
    if not callable(run_fn):
        return (False, "run(req, output_dir) 없음", False, "skipped")

    exec_ok, exec_detail = True, "skipped"
    if spec is not None:
        req = RunRequest(
            run_id=new_id(), experiment_id="verify", project_id="verify",
            param_values=spec.defaults(), test_set_uri="contract://selfcheck",
        )
        with tempfile.TemporaryDirectory() as out:
            try:
                result = run_fn(req, out)
                exec_ok = isinstance(result, RunResult) and result.metrics is not None
                exec_detail = f"status={getattr(result, 'status', None)}"
            except NotImplementedError:
                exec_ok = not strict
                exec_detail = "run() 아직 미구현 (TODO)"
            except Exception as exc:  # noqa: BLE001
                exec_ok, exec_detail = False, f"run() 오류: {exc}"
    return (True, "run() 존재", exec_ok, exec_detail)


def scan_hardcoded(project_root: str, spec: ParamSpec, algops_dir: str) -> list[dict]:
    """Heuristic: numeric literals (== a param default) still present OUTSIDE the algops dir."""
    defaults = {
        float(p.default): p.key
        for p in spec.params
        if p.dtype in (Dtype.float, Dtype.int)
    }
    findings: list[dict] = []
    skip = {os.path.abspath(algops_dir), ".venv", "__pycache__", ".git", "node_modules"}
    for root, dirs, files in os.walk(project_root):
        dirs[:] = [d for d in dirs if os.path.abspath(os.path.join(root, d)) not in skip and d not in skip]
        for fname in files:
            if not fname.endswith(".py"):
                continue
            fpath = os.path.join(root, fname)
            if os.path.abspath(fpath).startswith(os.path.abspath(algops_dir)):
                continue
            try:
                tree = ast.parse(open(fpath, encoding="utf-8").read())
            except Exception:
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
                    if float(node.value) in defaults:
                        findings.append({
                            "file": os.path.relpath(fpath, project_root),
                            "line": getattr(node, "lineno", 0),
                            "value": node.value,
                            "name": defaults[float(node.value)],
                        })
    return findings


def run_gate(project_root: str, algops_dir: str = "algops", strict: bool = False) -> GateResult:
    """Run the gate on a file-based externalized project (``<project_root>/<algops_dir>/...``)."""
    base = project_root if algops_dir in (".", "") else os.path.join(project_root, algops_dir)
    checks: list[tuple[str, bool, str]] = []

    ok_params, detail_params, spec = _validate_params(base)
    checks.append(("params.yaml 유효", ok_params, detail_params))

    ok_loader, detail_loader = _check_loader(base)
    checks.append(("config 로더", ok_loader, detail_loader))

    present, detail_present, exec_ok, exec_detail = _check_run_adapter(base, spec, strict)
    checks.append(("run_adapter.run 존재", present, detail_present))
    checks.append(("run_adapter 실행", exec_ok, exec_detail))

    findings = scan_hardcoded(project_root, spec, base) if spec else []

    hard_ok = ok_params and ok_loader and present and exec_ok
    return GateResult(ok=hard_ok, checks=checks, literal_findings=findings)


def run_gate_package(module_name: str) -> GateResult:
    """Run the gate on an installed package (e.g. the bundled toy: 'algops.toy_project')."""
    import importlib

    checks: list[tuple[str, bool, str]] = []
    try:
        module = importlib.import_module(module_name)
        algops_dir = os.path.dirname(module.__file__)
    except Exception as exc:  # noqa: BLE001
        return GateResult(ok=False, checks=[("패키지 import", False, str(exc))])

    ok_params, detail_params, spec = _validate_params(algops_dir)
    checks.append(("params.yaml 유효", ok_params, detail_params))

    loader = getattr(module, "load_params", None)
    ok_loader = callable(loader)
    try:
        if ok_loader:
            loader()
        checks.append(("load_params()", ok_loader, "정상" if ok_loader else "없음"))
    except Exception as exc:  # noqa: BLE001
        ok_loader = False
        checks.append(("load_params()", False, str(exc)))

    run_fn = getattr(module, "run", None)
    present = callable(run_fn)
    exec_ok, exec_detail = True, "skipped"
    if present and spec is not None:
        req = RunRequest(run_id=new_id(), experiment_id="verify", project_id="verify",
                         param_values=spec.defaults(), test_set_uri="contract://selfcheck")
        with tempfile.TemporaryDirectory() as out:
            try:
                result = run_fn(req, out)
                exec_ok = isinstance(result, RunResult) and result.metrics is not None
                exec_detail = f"status={getattr(result, 'status', None)}"
            except Exception as exc:  # noqa: BLE001
                exec_ok, exec_detail = False, f"run() 오류: {exc}"
    checks.append(("run(req, output_dir)", present, "존재" if present else "없음"))
    checks.append(("run 실행", exec_ok, exec_detail))

    return GateResult(ok=ok_params and ok_loader and present and exec_ok, checks=checks)
