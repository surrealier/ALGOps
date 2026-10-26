"""Execute one run of one algorithm through the language-agnostic protocol (``algops.run/v2``).

    runner                                   algorithm program (any language)
    ------                                   --------------------------------
    validate + clamp params (ParamSpec)
    write  <run>/request.json   ───────────▶ read $ALGOPS_REQUEST (or {request} argv placeholder)
           <run>/params.json                 ... or flat $ALGOPS_PARAMS_FILE / $ALGOPS_PARAM_<KEY>
    start entrypoint (cwd, env, timeout)     evaluate on the suite, write CSVs into output_dir
    read   <run>/result.json   ◀─────────── write $ALGOPS_RESULT  (or print ::algops-result::{json})
    derive preset metrics, collect artifacts

The same engine serves the distributed slave, ``algops run`` and ``algops test`` — so a repo's
own CI, a developer's laptop and the master all evaluate an algorithm identically.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import sys
import time
from dataclasses import dataclass, field
from typing import Any

from algops.core import (
    AlgorithmSpec,
    RepoManifest,
    RunRequest,
    RunResult,
    RunStatus,
    SuiteSpec,
    complete_metrics,
    normalize_metrics,
)
from algops.sdk import PROTOCOL_REQUEST, RESULT_MARKER, SDK_FILE

from .process import EnvPolicy, run_process

MAX_ARTIFACT_BYTES = 64 * 1024 * 1024
_BINARY_COUNTS = ("tp", "fp", "fn", "tn")
_PLACEHOLDER = re.compile(r"\{(request|result|output_dir|params|suite_path|suite_uri|run_id|repo_root|param:[^}]+)\}")


@dataclass
class RunOutcome:
    result: RunResult
    files: dict[str, bytes] = field(default_factory=dict)  # output files, "relative/path" -> bytes
    log: str = ""
    run_dir: str = ""


_VAR = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}|\$([A-Za-z_][A-Za-z0-9_]*)")


class SuiteResolutionError(ValueError):
    pass


def expand_vars(text: str, env: dict[str, str] | None = None) -> tuple[str, list[str]]:
    """Expand ``${VAR}``, ``${VAR:-default}`` and ``$VAR``; returns (text, unset variable names)."""
    env = dict(os.environ) if env is None else env
    missing: list[str] = []

    def repl(m: re.Match) -> str:
        name = m.group(1) or m.group(3)
        if name in env and env[name] != "":
            return env[name]
        if m.group(2) is not None:
            return m.group(2)
        missing.append(name)
        return m.group(0)

    return _VAR.sub(repl, text), missing


def resolve_suite_path(suite: SuiteSpec, repo_root: str, env: dict[str, str] | None = None) -> str | None:
    """Local filesystem path for a suite uri, or None for remote schemes / empty uris.

    ``${VAR}``, ``${VAR:-default}``, ``$VAR`` and ``~`` expand on the runner host (its environment
    plus ``env.vars``), so each machine can keep datasets wherever it likes. Raises
    SuiteResolutionError when a variable is unset or the resolved local path does not exist.
    """
    uri = (suite.uri or "").strip()
    if not uri:
        return None
    if "://" in uri and not uri.startswith("file://"):
        return None
    uri = uri.removeprefix("file://")
    expanded, missing = expand_vars(uri, env)
    if missing:
        raise SuiteResolutionError(
            f"suite {suite.id!r}: environment variable(s) {', '.join(sorted(set(missing)))} not set "
            f"on this runner (uri {suite.uri!r})")
    expanded = os.path.expanduser(expanded)
    path = os.path.abspath(expanded if os.path.isabs(expanded) else os.path.join(repo_root, expanded))
    if not os.path.exists(path):
        raise SuiteResolutionError(f"suite {suite.id!r}: path does not exist on this runner: {path}")
    return path


_ANY_PLACEHOLDER = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*(?::[^}]*)?)\}")
_PLACEHOLDER_NAMES = {"request", "result", "output_dir", "params", "suite_path", "suite_uri", "run_id", "repo_root"}


def placeholders_in(texts: list[str]) -> tuple[list[str], list[str]]:
    """(unknown placeholder names, parameter keys used via {param:key}) in command strings.

    Shell braces that are not identifiers (``{print $1}``, ``${VAR}``) are not placeholders.
    """
    unknown: list[str] = []
    keys: list[str] = []
    for text in texts:
        for m in _ANY_PLACEHOLDER.finditer(text):
            if m.start() > 0 and text[m.start() - 1] == "$":
                continue  # ${VAR} is a shell variable
            name = m.group(1)
            if name.startswith("param:"):
                keys.append(name[len("param:"):])
            elif ":" not in name and name not in _PLACEHOLDER_NAMES:
                unknown.append(name)
    return unknown, keys


def _env_key(key: str) -> str:
    return "ALGOPS_PARAM_" + re.sub(r"[^A-Za-z0-9]+", "_", key).strip("_").upper()


def _fmt_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    return str(v)


def _substitute(text: str, values: dict[str, str], params: dict[str, Any], quote: bool) -> str:
    def repl(m: re.Match) -> str:
        name = m.group(1)
        if name.startswith("param:"):
            key = name[len("param:"):]
            if key not in params:
                raise ValueError(f"placeholder {{param:{key}}} names an unknown parameter")
            val = _fmt_value(params[key])
        else:
            val = values.get(name) or ""
        return shlex.quote(val) if quote else val

    return _PLACEHOLDER.sub(repl, text)


def _python_for(manifest: RepoManifest, repo_root: str, default_python: str) -> str:
    py = manifest.env.python
    if not py:
        return default_python
    py = os.path.expanduser(os.path.expandvars(py))
    if os.sep in py or "/" in py:
        return py if os.path.isabs(py) else os.path.join(repo_root, py)
    return py  # a name on PATH, e.g. "python3.11"


def _read_result(result_path: str, output: str) -> tuple[dict | None, str | None]:
    """result.json first; else the last ``::algops-result::{json}`` line on stdout."""
    if os.path.isfile(result_path):
        try:
            with open(result_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return (data, None) if isinstance(data, dict) else (None, "result.json is not an object")
        except Exception as exc:  # noqa: BLE001
            return None, f"result.json is not valid JSON: {exc}"
    for line in reversed(output.splitlines()):
        idx = line.find(RESULT_MARKER)
        if idx >= 0:
            try:
                data = json.loads(line[idx + len(RESULT_MARKER):])
                return (data, None) if isinstance(data, dict) else (None, "result marker is not an object")
            except Exception as exc:  # noqa: BLE001
                return None, f"result marker is not valid JSON: {exc}"
    return None, None


def _collect(out_dir: str, run_dir: str) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    for root, _dirs, names in os.walk(out_dir):
        for name in sorted(names):
            path = os.path.join(root, name)
            rel = os.path.relpath(path, out_dir).replace(os.sep, "/")
            try:
                if os.path.getsize(path) > MAX_ARTIFACT_BYTES:
                    continue
                with open(path, "rb") as fh:
                    files[rel] = fh.read()
            except OSError:
                continue
    for name in ("request.json", "result.json", "params.json"):
        path = os.path.join(run_dir, name)
        if os.path.isfile(path):
            with open(path, "rb") as fh:
                files[f"_algops/{name}"] = fh.read()
    return files


def _fail(req: RunRequest, kind: str, error: str, started: float, **kw: Any) -> RunResult:
    return RunResult(
        run_id=req.run_id, status=RunStatus.failed, error=error, error_kind=kind,  # type: ignore[arg-type]
        duration_s=round(time.monotonic() - started, 6), **kw,
    )


async def execute_run(
    req: RunRequest,
    *,
    repo_root: str,
    manifest: RepoManifest,
    work_dir: str,
    commit_sha: str | None = None,
    default_python: str | None = None,
    extra_env: dict[str, str] | None = None,
    keep_run_dir: bool = True,
    env_policy: EnvPolicy | None = None,
) -> RunOutcome:
    """Evaluate ``req`` against the checkout at ``repo_root`` and return the outcome.

    Never raises for program/protocol problems — those become a failed RunResult with an
    ``error_kind`` so the master can tell a broken algorithm from a broken runner.
    """
    started = time.monotonic()
    sha = commit_sha or (req.repo.sha if req.repo else None)
    try:
        algo: AlgorithmSpec = manifest.algorithm(req.algorithm_id)
        suite = manifest.suite(req.suite_id)
        if suite.id not in [s.id for s in manifest.suites_for(algo.id)]:
            raise KeyError(f"suite {suite.id!r} is not enabled for algorithm {algo.id!r}")
    except KeyError as exc:
        return RunOutcome(_fail(req, "params", str(exc).strip("'\""), started, commit_sha=sha))

    try:
        params = algo.spec().effective(req.param_values, strict=True)
    except ValueError as exc:
        return RunOutcome(_fail(req, "params", f"invalid parameters: {exc}", started, commit_sha=sha))

    run_dir = os.path.abspath(os.path.join(work_dir, "runs", req.run_id))
    out_dir = os.path.join(run_dir, "out")
    if os.path.isdir(run_dir):
        shutil.rmtree(run_dir, ignore_errors=True)
    os.makedirs(out_dir, exist_ok=True)
    request_path = os.path.join(run_dir, "request.json")
    result_path = os.path.join(run_dir, "result.json")
    params_path = os.path.join(run_dir, "params.json")
    sdk_dir = os.path.join(run_dir, "_sdk")
    os.makedirs(sdk_dir, exist_ok=True)
    shutil.copyfile(SDK_FILE, os.path.join(sdk_dir, "algops_sdk.py"))

    base_env = (env_policy or EnvPolicy()).environ()
    expand_env = dict(base_env)
    expand_env.update(manifest.env.vars)
    try:
        suite_path = resolve_suite_path(suite, repo_root, expand_env)
    except SuiteResolutionError as exc:
        return RunOutcome(_fail(req, "setup", str(exc), started, commit_sha=sha, param_values=params),
                          run_dir=run_dir)
    request = {
        "protocol": PROTOCOL_REQUEST,
        "run_id": req.run_id,
        "project_id": req.project_id,
        "experiment_id": req.experiment_id or None,
        "benchmark_id": req.benchmark_id,
        "kind": req.kind,
        "algorithm_id": algo.id,
        "suite": {"id": suite.id, "uri": suite.uri, "path": suite_path, "tags": suite.tags},
        "params": params,
        "output_dir": out_dir,
        "result_path": result_path,
        "repo_root": os.path.abspath(repo_root),
        "commit_sha": sha,
        "seed": req.seed,
    }
    with open(request_path, "w", encoding="utf-8") as fh:
        json.dump(request, fh, ensure_ascii=False, indent=2)
    with open(params_path, "w", encoding="utf-8") as fh:
        json.dump(params, fh, ensure_ascii=False, indent=2)

    env = dict(base_env)
    env.update(manifest.env.vars)
    env.update(algo.entrypoint.env)
    env.update(extra_env or {})
    env.update({
        "ALGOPS_REQUEST": request_path,
        "ALGOPS_RESULT": result_path,
        "ALGOPS_OUTPUT_DIR": out_dir,
        "ALGOPS_PARAMS_FILE": params_path,
        "ALGOPS_RUN_ID": req.run_id,
        "ALGOPS_ALGORITHM_ID": algo.id,
        "ALGOPS_SUITE_ID": suite.id,
        "ALGOPS_SUITE_URI": suite.uri or "",
        "ALGOPS_SUITE_PATH": suite_path or "",
        "ALGOPS_REPO_ROOT": os.path.abspath(repo_root),
        "ALGOPS_COMMIT_SHA": sha or "",
        "ALGOPS_SEED": str(req.seed),
        "PYTHONIOENCODING": "utf-8",
    })
    for key, value in params.items():
        env[_env_key(key)] = _fmt_value(value)
    env["PYTHONPATH"] = sdk_dir + (os.pathsep + env["PYTHONPATH"] if env.get("PYTHONPATH") else "")

    placeholders = {
        "request": request_path, "result": result_path, "output_dir": out_dir,
        "params": params_path, "suite_path": suite_path or "", "suite_uri": suite.uri or "",
        "run_id": req.run_id, "repo_root": os.path.abspath(repo_root),
    }
    ep = algo.entrypoint
    cwd = os.path.abspath(os.path.join(repo_root, ep.cwd or "."))
    shell = False
    try:
        if ep.python is not None:
            python = _python_for(manifest, repo_root, default_python or sys.executable)
            argv: list[str] | str = [python, "-m", "algops_sdk", "run", ep.python,
                                     "--request", request_path, "--result", result_path]
        elif isinstance(ep.command, str):
            argv = _substitute(ep.command, placeholders, params, quote=True)
            shell = True
        else:
            argv = [_substitute(a, placeholders, params, quote=False) for a in (ep.command or [])]
            if argv and not os.path.isabs(argv[0]) and ("/" in argv[0] or argv[0].startswith(".")):
                argv[0] = os.path.abspath(os.path.join(cwd, argv[0]))
    except ValueError as exc:
        return RunOutcome(_fail(req, "params", str(exc), started, commit_sha=sha, param_values=params),
                          run_dir=run_dir)

    timeout = float(req.timeout_s or algo.timeout_s)
    try:
        proc = await run_process(argv, cwd=cwd, env=env, timeout=timeout, shell=shell)
    except FileNotFoundError as exc:
        return RunOutcome(_fail(req, "program", f"entrypoint not found: {exc}", started,
                                commit_sha=sha, param_values=params), run_dir=run_dir)
    except PermissionError as exc:
        return RunOutcome(_fail(req, "program", f"entrypoint is not executable (chmod +x?): {exc}",
                                started, commit_sha=sha, param_values=params), run_dir=run_dir)
    except OSError as exc:
        return RunOutcome(_fail(req, "program", f"cannot start entrypoint: {exc}", started,
                                commit_sha=sha, param_values=params), run_dir=run_dir)
    log = proc.output
    files = _collect(out_dir, run_dir)
    files["_algops/log.txt"] = log.encode("utf-8")

    def done(result: RunResult) -> RunOutcome:
        if not keep_run_dir:
            shutil.rmtree(run_dir, ignore_errors=True)
        return RunOutcome(result=result, files=files, log=log, run_dir=run_dir)

    if proc.timed_out:
        return done(_fail(req, "timeout", f"timed out after {timeout:g}s", started,
                          commit_sha=sha, param_values=params))

    data, parse_error = _read_result(result_path, log)
    tail = "\n".join(log.strip().splitlines()[-20:])
    if data is None:
        msg = parse_error or f"no result.json written (exit code {proc.returncode})"
        kind = "protocol" if (parse_error or proc.returncode == 0) else "program"
        return done(_fail(req, kind, f"{msg}\n{tail}".strip(), started, commit_sha=sha, param_values=params))

    status = str(data.get("status", "ok")).lower()
    if status == "failed":
        return done(_fail(req, "program", str(data.get("error") or "program reported status=failed"),
                          started, commit_sha=sha, param_values=params, extra=data.get("extra") or {}))
    if status not in ("ok", "evaluated"):
        return done(_fail(req, "protocol", f"result status must be 'ok' or 'failed' (got {status!r})",
                          started, commit_sha=sha, param_values=params))
    if proc.returncode != 0:
        return done(_fail(req, "program", f"exit code {proc.returncode} after writing result\n{tail}",
                          started, commit_sha=sha, param_values=params))

    raw_metrics = data.get("metrics") or {}
    try:
        metrics = normalize_metrics(raw_metrics)
        counts = data.get("counts")
        if counts is not None:
            counts = {k: int(v) for k, v in counts.items()}
        elif "binary" in algo.metrics.presets and all(k in metrics for k in _BINARY_COUNTS):
            counts = {k: int(metrics[k]) for k in _BINARY_COUNTS}
        metrics = complete_metrics(metrics, counts)
    except Exception as exc:  # noqa: BLE001
        return done(_fail(req, "protocol", f"malformed metrics/counts: {exc}", started,
                          commit_sha=sha, param_values=params))
    dropped = sorted(str(k) for k in raw_metrics if str(k) not in metrics) if isinstance(raw_metrics, dict) else []
    if not metrics:
        why = f" (non-numeric values dropped: {dropped})" if dropped else ""
        return done(_fail(req, "protocol", f"result has no numeric metrics and no counts{why}", started,
                          commit_sha=sha, param_values=params))
    extra = dict(data.get("extra") or {})
    if dropped:
        extra["dropped_metrics"] = dropped

    telemetry = data.get("telemetry")
    manifest_uri = None
    if telemetry:
        cand = os.path.abspath(os.path.join(out_dir, telemetry))
        if cand.startswith(out_dir + os.sep) and os.path.isfile(cand):
            manifest_uri = cand
    n_items = data.get("n_items")
    return done(RunResult(
        run_id=req.run_id,
        status=RunStatus.evaluated,
        metrics=metrics,
        counts=counts,
        csv_manifest_uri=manifest_uri,
        n_items=int(n_items) if n_items is not None else None,
        commit_sha=sha,
        duration_s=round(time.monotonic() - started, 6),
        param_values=params,
        extra=extra,
    ))
