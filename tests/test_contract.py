"""M4: externalization contract — bundle materialization, introspection, verify gate."""

from __future__ import annotations

import os
import textwrap

from algops.contract import (
    materialize_bundle,
    run_gate,
    run_gate_package,
    scan_candidates,
    write_understanding,
)

VALID_PARAMS_YAML = textwrap.dedent(
    """
    meta:
      algops_contract_version: "1.0"
      modality: tabular
      param_spec_version: 1
    params:
      - key: detector.confidence_threshold
        dtype: float
        default: 0.45
        range: [0.05, 0.95]
        step: 0.05
        stage: decision
        influence: { fp: high, fn: high }
    """
).strip()

CONFORMING_RUN_ADAPTER = textwrap.dedent(
    """
    from algops.core import Metrics, RunResult, RunStatus
    def run(req, output_dir):
        return RunResult(run_id=req.run_id, status=RunStatus.evaluated,
                         metrics=Metrics.from_counts(5, 1, 1, 5), n_items=12)
    """
).strip()


def _write(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, "w", encoding="utf-8").write(text + "\n")


def test_materialize_bundle(tmp_path):
    files = materialize_bundle(str(tmp_path))
    names = {os.path.basename(f) for f in files}
    assert {"AGENTS.md", "CLAUDE.md", "params.schema.json"} <= names
    assert os.path.exists(os.path.join(tmp_path, "algops", "_templates", "run_adapter.py.tmpl"))
    # CLAUDE.md must @import AGENTS.md (Claude Code does not read AGENTS.md on its own).
    assert "@AGENTS.md" in open(os.path.join(tmp_path, "CLAUDE.md"), encoding="utf-8").read()


def test_gate_passes_on_bundled_toy():
    result = run_gate_package("algops.toy_project")
    assert result.ok, result.render()


def test_gate_passes_on_conforming_project(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, "algops", "params.yaml"), VALID_PARAMS_YAML)
    _write(os.path.join(root, "algops", "run_adapter.py"), CONFORMING_RUN_ADAPTER)
    result = run_gate(root, algops_dir="algops")
    assert result.ok, result.render()


def test_gate_fails_on_messy_project(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, "detector.py"), "def filt(x):\n    return x.score >= 0.45  # hardcoded")
    result = run_gate(root, algops_dir="algops")
    assert not result.ok  # no algops/params.yaml -> gate fails
    assert any("params.yaml" in detail for _, ok, detail in result.checks if not ok)


def test_gate_fails_on_invalid_params(tmp_path):
    root = str(tmp_path)
    # default out of range -> ParamSpec validation fails
    bad = VALID_PARAMS_YAML.replace("default: 0.45", "default: 9.9")
    _write(os.path.join(root, "algops", "params.yaml"), bad)
    _write(os.path.join(root, "algops", "run_adapter.py"), CONFORMING_RUN_ADAPTER)
    result = run_gate(root, algops_dir="algops")
    assert not result.ok


def test_introspect_finds_candidates(tmp_path):
    root = str(tmp_path)
    _write(os.path.join(root, "pipeline.py"),
           "CONF_THRESHOLD = 0.45\nROUNDNESS = 0.78\n\ndef f(x):\n    return x > 0.45")
    cands = scan_candidates(root)
    values = {c.value for c in cands}
    assert 0.45 in values and 0.78 in values
    path = write_understanding(root)
    assert os.path.exists(path)
    assert "0.45" in open(path, encoding="utf-8").read()
