"""The language-agnostic run protocol: SDK, shell/C entrypoints, failure modes, env setup."""

from __future__ import annotations

import json
import os
import shutil
import sys

import pytest

from algops.core import RepoRef, RunRequest, RunStatus, load_manifest, new_id, parse_manifest
from algops.runner import RunnerEngine, execute_run
from algops.runner.workspace import Workspace

from .conftest import EXAMPLES


def _req(name: str, algo: str, suite: str, **params) -> RunRequest:
    return RunRequest(run_id=new_id(), project_id=name, repo=RepoRef(kind="local", name=name),
                      algorithm_id=algo, suite_id=suite, param_values=params, kind="adhoc")


@pytest.fixture
def engine(tmp_path):
    return RunnerEngine(str(tmp_path / "work"), local_projects={
        "py": os.path.join(EXAMPLES, "py-multi-algo"),
    })


async def test_python_sdk_entrypoint_with_telemetry(engine):
    out = await engine.run(_req("py", "detector", "det-smoke", **{"detector.score_threshold": 0.6}))
    r = out.result
    assert r.status == RunStatus.evaluated, r.error
    assert r.counts and sum(r.counts.values()) == 240
    assert {"precision", "recall", "fp_rate", "latency_us"} <= set(r.metrics)
    assert r.param_values["detector.score_threshold"] == 0.6
    assert r.param_values["detector.area_rule"] == "strict"  # defaults filled in by the runner
    assert r.csv_manifest_uri and r.csv_manifest_uri.endswith("manifest.json")
    manifest = json.loads(out.files["manifest.json"])
    assert [s["stage"] for s in manifest["stages"]] == ["ai_inference", "postproc_shape", "decision"]
    assert "_algops/request.json" in out.files and "_algops/log.txt" in out.files
    assert out.files["decision.csv"].decode().startswith("run_id,item_id,frame_index,entity_id,stage,dec_kept")


async def test_regression_algorithm_metrics(engine):
    smooth = await engine.run(_req("py", "smoother", "sig-smoke", **{"smoother.window": 5, "smoother.alpha": 0.5}))
    raw = await engine.run(_req("py", "smoother", "sig-smoke", **{"smoother.window": 1, "smoother.alpha": 1.0}))
    assert smooth.result.status == raw.result.status == RunStatus.evaluated
    assert smooth.result.metrics["mae"] < raw.result.metrics["mae"]  # filtering helps
    assert "tp" not in smooth.result.metrics  # no binary preset on this algorithm


async def test_unknown_param_and_suite_are_rejected_before_running(engine):
    bad = await engine.run(_req("py", "detector", "det-smoke", **{"detector.typo": 1}))
    assert bad.result.status == RunStatus.failed and bad.result.error_kind == "params"
    wrong_suite = await engine.run(_req("py", "detector", "sig-smoke"))  # not enabled for detector
    assert wrong_suite.result.error_kind == "params"


def _repo(tmp_path, manifest_yaml: str, files: dict[str, str]) -> str:
    root = tmp_path / "repo"
    root.mkdir()
    (root / "algops.yaml").write_text(manifest_yaml, encoding="utf-8")
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return str(root)


_SHELL = """
algops: 2
suites: [{id: s, uri: data}]
algorithms:
  - id: sh
    params:
      - {key: sh.k, dtype: int, default: 2, range: [0, 10], step: 1, stage: decision}
    entrypoint:
      command: "sh run.sh {param:sh.k} {result} {suite_path}"
    metrics: [{name: k2, direction: maximize}]
"""

_RUN_SH = """set -eu
test -d "$3"
printf '{"status":"ok","metrics":{"k2":%s,"env_k":%s}}' "$(( $1 * 2 ))" "$ALGOPS_PARAM_SH_K" > "$2"
"""


async def test_shell_command_with_placeholders(tmp_path):
    root = _repo(tmp_path, _SHELL, {"data/.keep": "", "run.sh": _RUN_SH})
    m = load_manifest(root)
    out = await execute_run(RunRequest(run_id=new_id(), project_id="p", algorithm_id="sh", suite_id="s",
                                       param_values={"sh.k": 7}), repo_root=root, manifest=m,
                            work_dir=str(tmp_path / "w"))
    assert out.result.status == RunStatus.evaluated, out.result.error
    assert out.result.metrics == {"k2": 14.0, "env_k": 7.0}


def _py_cmd(code: str) -> str:
    return f"""
algops: 2
algorithms:
  - id: x
    timeout_s: 2
    entrypoint:
      command: [{json.dumps(sys.executable)}, "-c", {json.dumps(code)}]
"""


@pytest.mark.parametrize("code, kind, needle", [
    ("import time; time.sleep(30)", "timeout", "timed out"),
    ("print('hello')", "protocol", "no result.json"),
    ("import os; open(os.environ['ALGOPS_RESULT'],'w').write('{not json')", "protocol", "not valid JSON"),
    ("raise SystemExit(3)", "program", "exit code 3"),
    ("import os,sys; open(os.environ['ALGOPS_RESULT'],'w').write('{\"metrics\":{\"a\":1}}'); sys.exit(1)",
     "program", "after writing result"),
    ("import os; open(os.environ['ALGOPS_RESULT'],'w').write('{\"status\":\"ok\",\"metrics\":{}}')",
     "protocol", "no metrics"),
    ("import os; open(os.environ['ALGOPS_RESULT'],'w').write('{\"status\":\"failed\",\"error\":\"boom\"}')",
     "program", "boom"),
])
async def test_failure_modes_are_classified(tmp_path, code, kind, needle):
    root = _repo(tmp_path, _py_cmd(code), {})
    out = await execute_run(RunRequest(run_id=new_id(), project_id="p", algorithm_id="x"),
                            repo_root=root, manifest=load_manifest(root), work_dir=str(tmp_path / "w"))
    assert out.result.status == RunStatus.failed
    assert out.result.error_kind == kind, out.result.error
    assert needle in (out.result.error or "")


async def test_stdout_marker_fallback(tmp_path):
    code = "print('::algops-result::' + '{\"status\":\"ok\",\"metrics\":{\"m\":0.5}}')"
    root = _repo(tmp_path, _py_cmd(code), {})
    out = await execute_run(RunRequest(run_id=new_id(), project_id="p", algorithm_id="x"),
                            repo_root=root, manifest=load_manifest(root), work_dir=str(tmp_path / "w"))
    assert out.result.status == RunStatus.evaluated and out.result.metrics == {"m": 0.5}


async def test_suite_uri_expands_runner_env(tmp_path, monkeypatch):
    data = tmp_path / "datasets" / "full"
    data.mkdir(parents=True)
    (data / "x.txt").write_text("1", encoding="utf-8")
    monkeypatch.setenv("ALGOPS_TEST_DATASETS", str(tmp_path / "datasets"))
    code = ("import os,json; p=os.environ['ALGOPS_SUITE_PATH']; "
            "open(os.environ['ALGOPS_RESULT'],'w').write(json.dumps({'metrics':{'n':len(os.listdir(p))}}))")
    manifest = _py_cmd(code) + "suites:\n  - {id: full, uri: '${ALGOPS_TEST_DATASETS}/full'}\n"
    root = _repo(tmp_path, manifest, {})
    out = await execute_run(RunRequest(run_id=new_id(), project_id="p", algorithm_id="x", suite_id="full"),
                            repo_root=root, manifest=load_manifest(root), work_dir=str(tmp_path / "w"))
    assert out.result.metrics == {"n": 1.0}, out.result.error


async def test_env_setup_runs_once_per_revision_and_failure_is_reported(tmp_path):
    counter = tmp_path / "count.txt"
    manifest = f"""
algops: 2
env:
  setup: ["echo x >> {counter}"]
algorithms:
  - id: a
    entrypoint:
      command: [{json.dumps(sys.executable)}, "-c", "import os; open(os.environ['ALGOPS_RESULT'],'w').write('{{\\"metrics\\":{{\\"v\\":1}}}}')"]
"""
    root = _repo(tmp_path, manifest, {})
    eng = RunnerEngine(str(tmp_path / "w"), local_projects={"r": root})
    for _ in range(3):
        out = await eng.run(_req("r", "a", "default"))
        assert out.result.status == RunStatus.evaluated, out.result.error
    assert counter.read_text().count("x") == 1  # cached by (path, revision, setup)
    (tmp_path / "repo" / "algops.yaml").write_text(manifest.replace("echo x", "exit 7 #"), encoding="utf-8")
    out = await eng.run(_req("r", "a", "default"))
    assert out.result.status == RunStatus.failed and out.result.error_kind == "setup"
    assert "_algops/setup.log" in out.files


async def test_c_example_compiles_in_setup_and_runs(tmp_path):
    if shutil.which("cc") is None:
        pytest.skip("no C compiler")
    src = os.path.join(EXAMPLES, "c-threshold")
    root = str(tmp_path / "c")
    shutil.copytree(src, root, ignore=shutil.ignore_patterns("build"))
    eng = RunnerEngine(str(tmp_path / "w"), local_projects={"c": root})
    one = await eng.run(_req("c", "filter", "full", **{"filter.min_streak": 1}))
    three = await eng.run(_req("c", "filter", "full", **{"filter.min_streak": 3}))
    assert one.result.status == three.result.status == RunStatus.evaluated, one.result.error
    assert three.result.metrics["fp"] < one.result.metrics["fp"]  # debounce removes spikes
    assert os.path.isfile(os.path.join(root, "build", "eval"))


async def test_local_revision_tracks_edits(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    (root / "a.txt").write_text("1", encoding="utf-8")
    first = (await Workspace.local_revision(str(root))).sha
    (root / "a.txt").write_text("22", encoding="utf-8")
    second = (await Workspace.local_revision(str(root))).sha
    assert first.startswith("local-") and first != second


def test_manifest_command_validation():
    m = parse_manifest({"algorithms": [{"id": "a", "entrypoint": {"python": "x.py:run"}}]})
    assert m.algorithm("a").entrypoint.python == "x.py:run"
