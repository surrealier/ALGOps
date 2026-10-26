"""Static validation (``algops validate``) and the executable gate (``algops verify``) for v2 repos.

``validate`` never runs user code: manifest schema, parameter specs, entrypoint files (and their
executable bit), command placeholders, metric declarations for gates/objectives, suite paths.
``verify`` additionally executes every algorithm once with its default parameters on its first
suite (``--all-suites``: every suite) through the real runner engine and requires a
schema-valid result that reports the objective and gate metrics — the externalization is done
only when this exits 0.
"""

from __future__ import annotations

import os
import shlex
from dataclasses import dataclass, field

from algops.core import ManifestError, RepoManifest, RunStatus, load_manifest


@dataclass
class Finding:
    level: str  # error | warn | ok
    where: str
    message: str


@dataclass
class ValidationReport:
    manifest: RepoManifest | None = None
    findings: list[Finding] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.manifest is not None and not any(f.level == "error" for f in self.findings)

    def add(self, level: str, where: str, message: str) -> None:
        self.findings.append(Finding(level, where, message))

    def render(self) -> str:
        icon = {"error": "XX", "warn": "!!", "ok": "ok"}
        head = ("✅ PASS" if self.ok else "❌ FAIL") + " — ALGOps contract v2"
        lines = [head]
        for f in self.findings:
            lines.append(f"  [{icon[f.level]}] {f.where}: {f.message}")
        return "\n".join(lines)


def validate_repo(repo_root: str) -> ValidationReport:
    rep = ValidationReport()
    try:
        manifest = load_manifest(repo_root)
    except ManifestError as exc:
        rep.add("error", "manifest", str(exc))
        return rep
    rep.manifest = manifest
    kind = "legacy v1 layout (algops/params.yaml)" if manifest.legacy else "algops.yaml"
    rep.add("ok", "manifest", f"{kind}: {len(manifest.algorithms)} algorithm(s), {len(manifest.suites)} suite(s)")

    from algops.runner.protocol import SuiteResolutionError, placeholders_in, resolve_suite_path

    env = dict(os.environ)
    env.update(manifest.env.vars)
    for suite in manifest.suites:
        try:
            resolve_suite_path(suite, repo_root, env)
        except SuiteResolutionError as exc:
            # Datasets may legitimately live only on runner hosts — warn, don't fail.
            rep.add("warn", f"suite {suite.id}", f"{exc} (fine if it exists on the runners)")

    for algo in manifest.algorithms:
        where = f"algorithm {algo.id}"
        spec = algo.spec()
        if not spec.params:
            rep.add("warn", where, "declares no parameters (benchmark-only; campaigns need params)")
        else:
            rep.add("ok", where, f"{len(spec.params)} parameter(s)")
        ep = algo.entrypoint
        if ep.python:
            target = ep.python.rsplit(":", 1)[0]
            if target.endswith(".py") or "/" in target:
                path = os.path.join(repo_root, target)
                if not os.path.isfile(path):
                    rep.add("error", where, f"python entrypoint file not found: {target}")
        elif isinstance(ep.command, list) and ep.command:
            exe = ep.command[0]
            if "/" in exe or exe.startswith("."):
                path = os.path.join(repo_root, ep.cwd or ".", exe)
                if not os.path.exists(path):
                    level = "warn" if manifest.env.setup else "error"
                    rep.add(level, where, f"command {exe} not found"
                            + (" (built by env.setup?)" if level == "warn" else ""))
                elif os.name != "nt" and not os.access(path, os.X_OK):
                    rep.add("error", where, f"command {exe} is not executable (chmod +x, or run it via an "
                                            "interpreter, e.g. [\"sh\", \"{exe}\"])")
        elif isinstance(ep.command, str):
            try:
                shlex.split(ep.command)
            except ValueError as exc:
                rep.add("error", where, f"unparseable shell command: {exc}")
        if ep.command is not None:
            texts = ep.command if isinstance(ep.command, list) else [ep.command]
            known, params_used = placeholders_in(texts)
            for name in known:
                rep.add("warn", where, f"{{{name}}} is not a placeholder — it is passed to the program literally")
            for key in params_used:
                if key not in spec.by_key():
                    rep.add("error", where, f"placeholder {{param:{key}}} names an undeclared parameter")
        declared = algo.metric_specs()
        if not declared:
            rep.add("warn", where, "no metrics declared (add `metrics: { presets: [...], declare: [...] }`)")
        if algo.objective is not None:
            for name in [algo.objective.metric, *[c.metric for c in algo.objective.constraints]]:
                if name not in declared:
                    rep.add("error", where, f"objective metric {name!r} is not declared in metrics (typo?)")
        elif algo.default_objective() is None:
            rep.add("warn", where, "no objective (campaigns will require one explicitly)")
        for g in algo.gates:
            if g.metric not in declared:
                rep.add("error", where, f"gate metric {g.metric!r} is not declared in metrics (typo?)")
    return rep


async def verify_repo(repo_root: str, *, work_dir: str | None = None,
                      algorithms: list[str] | None = None, all_suites: bool = False) -> ValidationReport:
    from algops.core import RepoRef, RunRequest, new_id
    from algops.runner.engine import RunnerEngine
    from algops.runner.localtest import default_work_dir

    rep = validate_repo(repo_root)
    if not rep.ok or rep.manifest is None:
        return rep
    manifest = rep.manifest
    name = manifest.name or "verify"
    engine = RunnerEngine(work_dir or default_work_dir(), local_projects={name: os.path.abspath(repo_root)})
    jobs = []
    for algo in manifest.algorithms:
        if algorithms and algo.id not in algorithms:
            continue
        suites = manifest.suites_for(algo.id)
        jobs += [(algo, su) for su in (suites if all_suites else suites[:1])]
    for algo, suite in jobs:
        req = RunRequest(run_id=new_id(), project_id=name, kind="adhoc",
                         repo=RepoRef(kind="local", name=name), algorithm_id=algo.id, suite_id=suite.id)
        out = await engine.run(req)
        where = f"run {algo.id}@{suite.id}"
        res = out.result
        if res.status != RunStatus.evaluated:
            rep.add("error", where, f"{res.error_kind}: {(res.error or '').strip()[:800]}")
            continue
        metrics = res.metrics or {}
        required = []
        obj = algo.default_objective()
        if obj is not None:
            required += [obj.metric, *[c.metric for c in obj.constraints]]
        required += [g.metric for g in algo.gates]
        missing = sorted({m for m in required if m not in metrics})
        if missing:
            rep.add("error", where, f"result does not report required metric(s): {missing}")
        else:
            shown = ", ".join(f"{k}={v:.4g}" for k, v in list(metrics.items())[:6])
            rep.add("ok", where, f"evaluated in {res.duration_s:.2f}s — {shown}")
    return rep
