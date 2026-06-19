"""Resolve the target project's run-adapter + param spec, and execute one run.

For M0 the default is the bundled toy project. If ``project_path`` points at a real
externalized project (M4 output), we dynamically load ``<project>/algops/run_adapter.py`` and
``<project>/algops/params.yaml`` — the slave lives next to the code, so importing it is fine.
"""

from __future__ import annotations

import importlib.util
import os
from collections.abc import Callable

import yaml

from algops.core import ParamSpec, RunRequest, RunResult


def resolve_project(project_path: str | None) -> tuple[Callable[[RunRequest, str], RunResult], ParamSpec]:
    if not project_path:
        from algops.toy_project import load_param_spec, run

        return run, load_param_spec()

    adapter_file = os.path.join(project_path, "algops", "run_adapter.py")
    yaml_file = os.path.join(project_path, "algops", "params.yaml")
    spec = importlib.util.spec_from_file_location("target_run_adapter", adapter_file)
    if spec is None or spec.loader is None:
        raise FileNotFoundError(f"run_adapter not found: {adapter_file}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    raw = yaml.safe_load(open(yaml_file, encoding="utf-8")) or {}
    pspec = ParamSpec(
        version=int(raw.get("meta", {}).get("param_spec_version", 1)),
        modality=str(raw.get("meta", {}).get("modality", "tabular")),
        params=raw.get("params", []),
    )
    return module.run, pspec


def execute_run(
    run_fn: Callable[[RunRequest, str], RunResult], req: RunRequest, work_dir: str
) -> tuple[RunResult, dict[str, bytes]]:
    """Run the adapter and return (result, {filename: bytes}) for every emitted artifact."""
    out_dir = os.path.join(work_dir, req.run_id)
    result = run_fn(req, out_dir)
    files: dict[str, bytes] = {}
    if os.path.isdir(out_dir):
        for name in sorted(os.listdir(out_dir)):
            path = os.path.join(out_dir, name)
            if os.path.isfile(path):
                with open(path, "rb") as fh:
                    files[name] = fh.read()
    return result, files
