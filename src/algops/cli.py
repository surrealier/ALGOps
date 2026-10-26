"""``algops`` — the command-line entry point.

Repository-side (no master needed):
    algops init      scaffold algops.yaml + params + entrypoint + sample suite
    algops validate  static checks of the manifest/params/entrypoints
    algops verify    validate + execute every algorithm once (the externalization gate)
    algops run       run one algorithm on one suite (``--set key=value`` overrides)
    algops test      run every algorithm × suite, apply gates (+ ``--baseline`` report), exit 1 on failure
    algops introspect  list hardcoded numeric literals (externalization candidates)

Control plane:
    algops ci        trigger a benchmark on a master and wait for its verdict (exit 1 on failure)
    algops master    start the master            algops slave   start a runner
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from typing import Any


def _utf8() -> None:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")  # readable Korean on cp949 consoles
        except Exception:  # noqa: BLE001
            pass


def _parse_sets(items: list[str] | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for item in items or []:
        if "=" not in item:
            raise ValueError(f"--set expects key=value, got {item!r}")
        key, raw = item.split("=", 1)
        try:
            value: Any = json.loads(raw)
        except json.JSONDecodeError:
            value = raw
        out[key.strip()] = value
    return out


def _fmt_metrics(metrics: dict[str, float] | None, keys: list[str] | None = None, n: int = 6) -> str:
    if not metrics:
        return "-"
    names = [k for k in (keys or []) if k in metrics] or list(metrics)[:n]
    return ", ".join(f"{k}={metrics[k]:.4g}" for k in names[:n])


_VERDICT_ICON = {"passed": "✅", "improved": "⬆️ ", "regressed": "⚠️ ", "gate_failed": "❌", "error": "💥"}


# --------------------------------------------------------------------------- commands


def cmd_init(a: argparse.Namespace) -> int:
    from algops.contract.scaffold import init_repo

    from algops.contract.scaffold import algorithm_snippet
    from algops.core.manifest import find_manifest

    existing = find_manifest(a.path)
    declared = False
    if existing is not None:
        with open(existing, encoding="utf-8") as fh:
            declared = f"id: {a.algorithm}" in fh.read()
    written, skipped = init_repo(a.path, algorithm=a.algorithm, lang=a.lang, name=a.name, force=a.force)
    for p in written:
        print("  +", os.path.relpath(p, a.path))
    for p in skipped:
        print("  =", os.path.relpath(p, a.path), "(exists, kept — use --force to overwrite)")
    if existing is not None and not a.force and not declared:
        print(f"\n{os.path.basename(existing)} already exists — add this algorithm under `algorithms:`:\n")
        print(algorithm_snippet(a.algorithm, a.lang))
        print("then: `algops validate` → `algops run -a %s` → `algops test`" % a.algorithm)
        return 0
    print("\nnext: `algops validate` → `algops run -a %s -s smoke` → `algops test`" % a.algorithm)
    return 0


def cmd_validate(a: argparse.Namespace) -> int:
    from algops.contract.validate import validate_repo

    rep = validate_repo(a.path)
    print(rep.render())
    return 0 if rep.ok else 1


def cmd_verify(a: argparse.Namespace) -> int:
    from algops.core.manifest import find_manifest

    if find_manifest(a.path) is None and os.path.isfile(os.path.join(a.path, "algops", "params.yaml")) \
            and not a.v2:
        # v1 layout: the original gate (params + loader + run_adapter self-check, in-process).
        from algops.contract.verify import run_gate

        res = run_gate(a.path, algops_dir="algops", strict=a.strict)
        print(res.render())
        return 0 if res.ok else 1
    from algops.contract.validate import verify_repo

    rep = asyncio.run(verify_repo(a.path, work_dir=a.work_dir, algorithms=a.algorithm or None,
                                  all_suites=a.all_suites))
    print(rep.render())
    return 0 if rep.ok else 1


def cmd_run(a: argparse.Namespace) -> int:
    from algops.core import RepoRef, RunRequest, load_manifest, new_id
    from algops.runner.engine import RunnerEngine
    from algops.runner.localtest import default_work_dir

    root = os.path.abspath(a.path)
    manifest = load_manifest(root)
    algo_id = a.algorithm or manifest.algorithms[0].id
    suite_id = a.suite or manifest.suites_for(algo_id)[0].id
    name = manifest.name or os.path.basename(root)
    engine = RunnerEngine(a.work_dir or default_work_dir(), local_projects={name: root})
    req = RunRequest(run_id=new_id(), project_id=name, kind="adhoc", repo=RepoRef(kind="local", name=name),
                     algorithm_id=algo_id, suite_id=suite_id, param_values=_parse_sets(a.set),
                     timeout_s=a.timeout)
    out = asyncio.run(engine.run(req))
    res = out.result
    notes = []
    for k, v in req.param_values.items():
        got = (res.param_values or {}).get(k)
        if res.param_values and got != v and not (isinstance(v, (int, float)) and not isinstance(v, bool)
                                                 and isinstance(got, (int, float)) and float(v) == float(got)):
            notes.append(f"{k}: {v!r} → {got!r} (clamped/coerced to the declared range/type)")
    if a.json:
        print(res.model_dump_json(indent=2))
    else:
        print(f"{algo_id}@{suite_id}  status={res.status.value}  ({res.duration_s or 0:.2f}s, sha={res.commit_sha})")
        if res.param_values:
            print("params :", ", ".join(f"{k}={v}" for k, v in res.param_values.items()))
        for n in notes:
            print("note   :", n)
        if res.metrics:
            print("metrics:", _fmt_metrics(res.metrics, n=12))
        if res.error:
            print(f"error  : [{res.error_kind}] {res.error}")
        if out.run_dir:
            print("run dir:", out.run_dir)
    if a.log and out.log:
        print("\n--- log ---\n" + out.log)
    return 0 if res.status.value == "evaluated" else 1


def cmd_test(a: argparse.Namespace) -> int:
    from algops.runner.localtest import load_report, run_local_tests

    baseline = load_report(a.baseline) if a.baseline else None

    def on_cell(c: dict) -> None:
        if a.quiet:
            return
        icon = _VERDICT_ICON.get(c["verdict"], "?")
        line = f"{icon} {c['verdict']:<11} {c['algorithm_id']}@{c['suite_id']:<14} {_fmt_metrics(c['metrics'])}"
        if baseline is not None and c["status"] == "evaluated" and not c.get("has_baseline"):
            line += "  (no baseline cell)"
        if c.get("error"):
            line += f"  [{c.get('error_kind')}] {c['error'].splitlines()[0][:200]}"
            rest = c["error"].splitlines()[1:]
            if rest and a.log:
                line += "\n      " + "\n      ".join(rest[-25:])
        if a.log and c.get("log_tail"):
            line += "\n      --- program output (tail) ---\n      " + c["log_tail"].replace("\n", "\n      ")
        for g in c.get("gates", []):
            if not g["passed"]:
                actual = "not reported" if g["actual"] is None else g["actual"]
                line += f"\n      gate {g['metric']} {g['op']} {g['value']} (actual {actual})"
        for m, d in (c.get("deltas") or {}).items():
            if d["status"] == "regressed":
                line += f"\n      regressed {m}: {d['baseline']:.4g} → {d['current']:.4g} (tol {d['tolerance']:.3g})"
        print(line, flush=True)

    doc = asyncio.run(run_local_tests(
        a.path, algorithms=a.algorithm or None, suites=a.suite or None, tags=a.tag or None,
        overrides=_parse_sets(a.set), baseline=baseline, jobs=a.jobs, work_dir=a.work_dir,
        rel_tolerance=a.tolerance, on_cell=on_cell, timeout_s=a.timeout,
    ))
    if a.json:
        with open(a.json, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, ensure_ascii=False, indent=2)
    s = doc["summary"]
    sha = doc["commit_sha"]
    shown = sha if (sha.startswith("local") or "-dirty" in sha) else sha[:12]
    print(f"\n{doc['project']} @ {shown} — {len(doc['cells'])} cell(s): "
          + ", ".join(f"{k}={v}" for k, v in s.items() if v) + f"  ({doc['duration_s']:.1f}s)")
    if "-dirty" in sha or sha.startswith("local"):
        print("note: uncommitted changes are included in this run")
    if not doc["cells"]:
        from algops.core import load_manifest

        m = load_manifest(a.path)
        tags = sorted({t for su in m.suites for t in su.tags})
        print("no cells matched the filters — algorithms: " + ", ".join(x.id for x in m.algorithms)
              + "; suites: " + ", ".join(x.id for x in m.suites) + ("; tags: " + ", ".join(tags) if tags else ""))
    return 0 if doc["ok"] else 1


def cmd_introspect(a: argparse.Namespace) -> int:
    from algops.contract.introspect import write_understanding

    print("작성:", write_understanding(a.path))
    return 0


def cmd_ci(a: argparse.Namespace) -> int:
    from algops.ci import run_ci

    return run_ci(
        master_url=a.master, token=a.token or os.environ.get("ALGOPS_API_TOKEN"),
        project=a.project, ref=a.ref, algorithms=a.algorithm or None, suites=a.suite or None,
        tags=a.tag or None, wait=not a.no_wait, timeout_s=a.timeout, json_out=a.json,
    )


def cmd_master(_: argparse.Namespace) -> int:
    from algops.master.__main__ import main

    main()
    return 0


def cmd_slave(_: argparse.Namespace) -> int:
    from algops.slave.__main__ import main

    main()
    return 0


# --------------------------------------------------------------------------- parser


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="algops", description="ALGOps — test & tune algorithms in any repository.")
    sub = p.add_subparsers(dest="cmd", required=True)

    def repo_arg(sp: argparse.ArgumentParser) -> None:
        sp.add_argument("path", nargs="?", default=".", help="repository root (default: .)")

    sp = sub.add_parser("init", help="scaffold a contract-v2 repository")
    repo_arg(sp)
    sp.add_argument("-a", "--algorithm", default="main")
    sp.add_argument("--lang", choices=["python", "command"], default="python")
    sp.add_argument("--name")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_init)

    sp = sub.add_parser("validate", help="static manifest/params/entrypoint checks")
    repo_arg(sp)
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("verify", help="validate + execute every algorithm once (exit 0 = conforms)")
    repo_arg(sp)
    sp.add_argument("-a", "--algorithm", action="append")
    sp.add_argument("--work-dir")
    sp.add_argument("--strict", action="store_true", help="v1 layout: NotImplementedError fails")
    sp.add_argument("--v2", action="store_true", help="force the v2 gate on a v1 layout")
    sp.add_argument("--all-suites", action="store_true", help="run every suite, not just each algorithm's first")
    sp.set_defaults(fn=cmd_verify)

    sp = sub.add_parser("run", help="run one algorithm on one suite")
    repo_arg(sp)
    sp.add_argument("-a", "--algorithm")
    sp.add_argument("-s", "--suite")
    sp.add_argument("--set", action="append", metavar="KEY=VALUE", help="parameter override (repeatable)")
    sp.add_argument("--timeout", type=float)
    sp.add_argument("--work-dir")
    sp.add_argument("--json", action="store_true", help="print the RunResult as JSON")
    sp.add_argument("--log", action="store_true", help="print the program output")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("test", help="run every algorithm × suite and judge (exit 1 on failure)")
    repo_arg(sp)
    sp.add_argument("-a", "--algorithm", action="append")
    sp.add_argument("-s", "--suite", action="append")
    sp.add_argument("-t", "--tag", action="append", help="only suites with this tag")
    sp.add_argument("--set", action="append", metavar="KEY=VALUE")
    sp.add_argument("--baseline", help="a previous `algops test --json` report to compare against")
    sp.add_argument("--tolerance", type=float, help="override every rel_tolerance")
    sp.add_argument("--json", metavar="FILE", help="write the report (usable as a future --baseline)")
    sp.add_argument("-j", "--jobs", type=int, default=1)
    sp.add_argument("--timeout", type=float, help="per-run timeout override (seconds)")
    sp.add_argument("--log", action="store_true", help="show program output / setup log of failing cells")
    sp.add_argument("--work-dir")
    sp.add_argument("-q", "--quiet", action="store_true")
    sp.set_defaults(fn=cmd_test)

    sp = sub.add_parser("introspect", help="list hardcoded numeric literals (externalization candidates)")
    repo_arg(sp)
    sp.set_defaults(fn=cmd_introspect)

    sp = sub.add_parser("ci", help="trigger a master benchmark and wait for the verdict")
    sp.add_argument("--master", default=os.environ.get("ALGOPS_MASTER_URL", "http://127.0.0.1:8000"))
    sp.add_argument("--token", help="API token (or $ALGOPS_API_TOKEN)")
    sp.add_argument("-p", "--project", action="append", help="project name or id (repeatable; default: all)")
    sp.add_argument("--ref", help="branch/tag/sha to benchmark (default: each project's default ref)")
    sp.add_argument("-a", "--algorithm", action="append")
    sp.add_argument("-s", "--suite", action="append")
    sp.add_argument("-t", "--tag", action="append")
    sp.add_argument("--no-wait", action="store_true")
    sp.add_argument("--timeout", type=float, default=3600.0)
    sp.add_argument("--json", metavar="FILE")
    sp.set_defaults(fn=cmd_ci)

    sp = sub.add_parser("master", help="start the master (same as algops-master)")
    sp.set_defaults(fn=cmd_master)
    sp = sub.add_parser("slave", help="start a runner (same as algops-slave)")
    sp.set_defaults(fn=cmd_slave)
    return p


def main(argv: list[str] | None = None) -> int:
    _utf8()
    args = build_parser().parse_args(argv)
    try:
        return int(args.fn(args) or 0)
    except KeyboardInterrupt:
        return 130
    except Exception as exc:  # noqa: BLE001 — user-facing CLI: concise error, exit 2
        from algops.core import ManifestError

        if isinstance(exc, (ManifestError, KeyError, ValueError, FileNotFoundError)):
            print(f"error: {exc}", file=sys.stderr)
            return 2
        raise


if __name__ == "__main__":
    raise SystemExit(main())
