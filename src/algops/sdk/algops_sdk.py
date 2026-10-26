"""ALGOps SDK — write an algorithm entrypoint in Python with zero dependencies.

This single file is **stdlib-only** (Python >= 3.8) on purpose: the runner copies it next to
every run and puts it on ``PYTHONPATH``, so a repository with its own virtualenv never has to
install ALGOps (or any of its dependencies) to be evaluated. Inside an environment that has
ALGOps installed the same API is available as ``algops.sdk``.

The contract it implements (``algops.run/v2``) is language-agnostic — see
``docs/protocol-v2.md``. In short: the runner writes ``request.json``, starts your program, and
your program writes ``result.json``::

    # algops/detector/run.py            (manifest:  entrypoint: { python: "algops/detector/run.py:run" })
    import algops_sdk as sdk

    def run(ctx: sdk.Context):
        thr = ctx.params["detector.confidence_threshold"]
        tp = fp = fn = tn = 0
        decisions = ctx.stage("decision", ["dec_kept", "dec_reason", "gt_match"])
        for item in load(ctx.suite_path):
            ...
            decisions.row(item_id, frame_index, entity_id, dec_kept=kept, dec_reason=why, gt_match=gt)
        return sdk.counts(tp=tp, fp=fp, fn=fn, tn=tn, latency_ms=12.5)

Run it directly while developing (``python -m algops_sdk run algops/detector/run.py:run
--request request.json``) or, better, through ``algops run`` which builds the request for you.
"""

from __future__ import annotations

import csv
import importlib
import importlib.util
import inspect
import json
import math
import os
import sys
import time
import traceback

PROTOCOL_REQUEST = "algops.run/v2"
PROTOCOL_RESULT = "algops.result/v2"
RESULT_MARKER = "::algops-result::"
JOIN_COLUMNS = ["run_id", "item_id", "frame_index", "entity_id", "stage"]

__all__ = [
    "PROTOCOL_REQUEST", "PROTOCOL_RESULT", "RESULT_MARKER", "JOIN_COLUMNS",
    "Context", "StageWriter", "counts", "result", "failed",
    "read_request", "write_result", "main", "run_target",
]


# --------------------------------------------------------------------------- results


def result(metrics=None, counts=None, n_items=None, telemetry=None, extra=None, status="ok",
           error=None):
    """Build a protocol result dict. ``metrics`` = any ``{name: number}``."""
    out = {"protocol": PROTOCOL_RESULT, "status": status, "metrics": dict(metrics or {})}
    if counts is not None:
        out["counts"] = {k: int(v) for k, v in counts.items()}
    if n_items is not None:
        out["n_items"] = int(n_items)
    if telemetry is not None:
        out["telemetry"] = telemetry
    if extra:
        out["extra"] = dict(extra)
    if error is not None:
        out["error"] = str(error)
    return out


def counts(tp=0, fp=0, fn=0, tn=0, n_items=None, **metrics):
    """Result from a confusion matrix (the runner derives precision/recall/f1/fp_rate/...)."""
    return result(metrics=metrics, counts={"tp": tp, "fp": fp, "fn": fn, "tn": tn},
                  n_items=n_items if n_items is not None else tp + fp + fn + tn)


def failed(error, **extra):
    return result(status="failed", error=error, extra=extra)


# --------------------------------------------------------------------------- telemetry


class StageWriter(object):
    """One telemetry CSV per pipeline stage, joined on ``entity_id`` (see AGENTS.md).

    Columns use typed prefixes: ``in_*`` config, ``ai_*`` model output, ``feat_*`` features,
    ``dfeat_*`` derived/windowed features, ``dec_*`` decisions, ``gt_*`` ground truth.
    """

    def __init__(self, ctx, stage, columns, depends_on=None, grain="entity"):
        self.ctx = ctx
        self.stage = stage
        self.columns = list(columns)
        self.depends_on = list(depends_on or [])
        self.grain = grain
        self.filename = stage + ".csv"
        self._fh = open(os.path.join(ctx.output_dir, self.filename), "w", newline="", encoding="utf-8")
        self._w = csv.writer(self._fh)
        self._w.writerow(JOIN_COLUMNS + self.columns)
        self.rows = 0

    def row(self, item_id, frame_index, entity_id, **values):
        unknown = set(values) - set(self.columns)
        if unknown:
            raise KeyError("stage %s: undeclared columns %s" % (self.stage, sorted(unknown)))
        self._w.writerow([self.ctx.run_id, item_id, frame_index, entity_id, self.stage]
                         + [_cell(values.get(c)) for c in self.columns])
        self.rows += 1

    def close(self):
        if not self._fh.closed:
            self._fh.close()


def _cell(v):
    if v is None:
        return ""
    if isinstance(v, bool):
        return int(v)
    if isinstance(v, float):
        return "%.6g" % v
    return v


# --------------------------------------------------------------------------- context


class Context(object):
    """Everything a run needs, parsed from ``request.json``."""

    def __init__(self, request):
        self.raw = request
        self.run_id = request.get("run_id", "")
        self.project_id = request.get("project_id", "")
        self.algorithm_id = request.get("algorithm_id", "default")
        suite = request.get("suite") or {}
        self.suite_id = suite.get("id", "default")
        self.suite_uri = suite.get("uri", "")
        self.suite_path = suite.get("path")  # local path when the suite resolves to one
        self.params = dict(request.get("params") or {})
        self.output_dir = request.get("output_dir") or os.getcwd()
        self.result_path = request.get("result_path")
        self.repo_root = request.get("repo_root") or os.getcwd()
        self.commit_sha = request.get("commit_sha")
        self.seed = int(request.get("seed") or 0)
        self.kind = request.get("kind", "adhoc")
        self._stages = []
        if not os.path.isdir(self.output_dir):
            os.makedirs(self.output_dir)

    def param(self, key, default=None):
        return self.params.get(key, default)

    def path(self, *parts):
        """A path inside this run's output directory (uploaded as run artifacts)."""
        return os.path.join(self.output_dir, *parts)

    def stage(self, stage, columns, depends_on=None, grain="entity"):
        writer = StageWriter(self, stage, columns, depends_on, grain)
        self._stages.append(writer)
        return writer

    def finish_telemetry(self, n_items=None):
        """Close stage CSVs and write ``manifest.json``; returns its filename (or None)."""
        if not self._stages:
            return None
        for w in self._stages:
            w.close()
        manifest = {
            "run_id": self.run_id,
            "param_values": self.params,
            "n_items": n_items if n_items is not None else max(w.rows for w in self._stages),
            "stages": [
                {"stage": w.stage, "csv": w.filename, "grain": w.grain, "depends_on": w.depends_on}
                for w in self._stages
            ],
        }
        with open(self.path("manifest.json"), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)
        return "manifest.json"


# --------------------------------------------------------------------------- io


def read_request(path=None):
    path = path or os.environ.get("ALGOPS_REQUEST")
    if not path:
        raise SystemExit("algops_sdk: no request (pass --request FILE or set ALGOPS_REQUEST)")
    with open(path, encoding="utf-8") as fh:
        req = json.load(fh)
    proto = req.get("protocol", PROTOCOL_REQUEST)
    if not str(proto).startswith("algops.run/"):
        raise SystemExit("algops_sdk: unsupported request protocol %r" % proto)
    return req


def _json_safe(obj):
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, dict):
        return {str(k): _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def write_result(res, path=None):
    path = path or os.environ.get("ALGOPS_RESULT")
    payload = json.dumps(_json_safe(res), ensure_ascii=False, indent=2)
    if not path:
        sys.stdout.write(RESULT_MARKER + json.dumps(_json_safe(res), ensure_ascii=False) + "\n")
        return
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        fh.write(payload)
    os.replace(tmp, path)


# --------------------------------------------------------------------------- normalization

_RESERVED = {"metrics", "counts", "n_items", "telemetry", "extra", "status", "error", "protocol"}


def _normalize(ret, ctx):
    """Accept the shapes an entrypoint may return and produce a protocol result dict."""
    if ret is None:
        return failed("entrypoint returned None (return sdk.result(...) / sdk.counts(...))")
    if hasattr(ret, "model_dump"):  # a v1 algops.core.RunResult (pydantic)
        ret = _from_v1(ret.model_dump(mode="json"), ctx)
    if not isinstance(ret, dict):
        return failed("entrypoint returned %s, expected a dict" % type(ret).__name__)
    if not (set(ret) & _RESERVED):  # a bare {metric: value} mapping
        ret = result(metrics=ret)
    ret = dict(ret)
    ret.setdefault("protocol", PROTOCOL_RESULT)
    ret.setdefault("status", "ok")
    ret.setdefault("metrics", {})
    if ret.get("telemetry") is None:
        tele = ctx.finish_telemetry(ret.get("n_items"))
        if tele:
            ret["telemetry"] = tele
    else:
        ctx.finish_telemetry(ret.get("n_items"))
    return ret


def _from_v1(d, ctx):
    status = "ok" if d.get("status") in ("evaluated", "ok") else "failed"
    tele = d.get("csv_manifest_uri")
    if tele and os.path.isabs(tele):
        try:
            rel = os.path.relpath(tele, ctx.output_dir)
            tele = rel if not rel.startswith("..") else None
        except ValueError:
            tele = None
    return result(metrics=d.get("metrics") or {}, counts=d.get("counts"), n_items=d.get("n_items"),
                  telemetry=tele, status=status, error=d.get("error"))


# --------------------------------------------------------------------------- loading targets


def _load_target(target, repo_root):
    """``path/to/file.py:func`` (relative to the repo root) or ``package.module:func``."""
    if ":" not in target:
        raise SystemExit("algops_sdk: target must be 'file.py:func' or 'module:func'")
    mod_part, func_name = target.rsplit(":", 1)
    if mod_part.endswith(".py") or os.sep in mod_part or "/" in mod_part:
        path = mod_part if os.path.isabs(mod_part) else os.path.join(repo_root, mod_part)
        path = os.path.abspath(path)
        # Make the target's directory and the repo root importable (sibling helper modules).
        for p in (os.path.dirname(path), os.path.abspath(repo_root)):
            if p not in sys.path:
                sys.path.insert(0, p)
        name = "algops_target_" + os.path.splitext(os.path.basename(path))[0]
        spec = importlib.util.spec_from_file_location(name, path)
        if spec is None or spec.loader is None:
            raise SystemExit("algops_sdk: cannot load %s" % path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        spec.loader.exec_module(module)
    else:
        if os.path.abspath(repo_root) not in sys.path:
            sys.path.insert(0, os.path.abspath(repo_root))
        module = importlib.import_module(mod_part)
    fn = getattr(module, func_name, None)
    if not callable(fn):
        raise SystemExit("algops_sdk: %s has no callable %r" % (mod_part, func_name))
    return fn


def _is_v1(fn):
    """v1 adapters are ``run(req, output_dir)``; v2 entrypoints are ``run(ctx)``."""
    try:
        params = [p for p in inspect.signature(fn).parameters.values()
                  if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)]
    except (TypeError, ValueError):
        return False
    return len(params) >= 2


def _v1_request(req):
    from algops.core import RunRequest  # v1 adapters need ALGOps installed (pydantic types)

    suite = req.get("suite") or {}
    return RunRequest(
        run_id=req.get("run_id", ""),
        experiment_id=req.get("experiment_id") or "",
        project_id=req.get("project_id", ""),
        param_values=req.get("params") or {},
        test_set_uri=suite.get("path") or suite.get("uri") or "",
        benchmark_id=req.get("benchmark_id"),
        algorithm_id=req.get("algorithm_id", "default"),
        suite_id=suite.get("id", "default"),
        seed=int(req.get("seed") or 0),
    )


def run_target(target, request):
    """Call an entrypoint for ``request`` and return the normalized protocol result."""
    ctx = Context(request)
    fn = _load_target(target, ctx.repo_root)
    started = time.time()
    if _is_v1(fn):
        ret = fn(_v1_request(request), ctx.output_dir)
    else:
        ret = fn(ctx)
    res = _normalize(ret, ctx)
    res.setdefault("extra", {})
    res["extra"].setdefault("sdk_wall_s", round(time.time() - started, 6))
    return res


def main(fn_or_target=None, argv=None):
    """CLI: ``python -m algops_sdk run <target> [--request FILE] [--result FILE]``.

    Or, at the bottom of your own script::

        if __name__ == "__main__":
            sdk.main(run)
    """
    argv = list(sys.argv[1:] if argv is None else argv)
    req_path = res_path = None
    if "--request" in argv:
        i = argv.index("--request")
        req_path = argv[i + 1]
        del argv[i:i + 2]
    if "--result" in argv:
        i = argv.index("--result")
        res_path = argv[i + 1]
        del argv[i:i + 2]
    target = fn_or_target
    if target is None:
        if len(argv) < 2 or argv[0] != "run":
            sys.stderr.write("usage: python -m algops_sdk run <file.py:func|module:func> "
                             "[--request FILE] [--result FILE]\n")
            return 2
        target = argv[1]
    request = read_request(req_path)
    res_path = res_path or request.get("result_path") or os.environ.get("ALGOPS_RESULT")
    try:
        if callable(target):
            ctx = Context(request)
            res = _normalize(target(ctx), ctx)
        else:
            res = run_target(target, request)
    except Exception as exc:  # noqa: BLE001 — report, never hang the runner
        traceback.print_exc()
        res = failed("%s: %s" % (type(exc).__name__, exc), traceback=traceback.format_exc()[-4000:])
    write_result(res, res_path)
    return 0 if res.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
