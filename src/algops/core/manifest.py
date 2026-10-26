"""The repository manifest — ``algops.yaml`` (contract v2).

One repository declares *N algorithms* and *M test suites*. Each algorithm has its own parameter
space, a language-agnostic entrypoint, declared metrics, absolute gates and a regression policy::

    algops: 2
    name: vision-postproc
    env:
      setup: ["uv sync"]                # once per (commit, setup) — cached by the runner
      python: .venv/bin/python          # interpreter for `python:` entrypoints (optional)
    suites:
      - { id: smoke, uri: data/smoke, tags: [fast] }
      - { id: full,  uri: "${DATASETS}/full" }      # env vars expand on the runner host
    algorithms:
      - id: detector
        params: algops/detector/params.yaml         # or an inline list of params
        entrypoint: { python: "algops/detector/run.py:run" }   # or { command: [...] }
        metrics: { presets: [binary], declare: [{ name: latency_ms, direction: minimize }] }
        gates: [{ metric: recall, op: ">=", value: 0.85 }]
        objective: { metric: fp_rate, target: 0.05, constraints: [{ metric: recall, op: ">=", value: 0.85 }] }

Repositories that predate v2 (``algops/params.yaml`` + ``algops/run_adapter.py``) load as a
synthesized manifest with one algorithm ``default`` and one suite ``default``.
"""

from __future__ import annotations

import os
import re
from typing import Any

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from .enums import GoalDirection
from .metrics import Constraint, MetricSpec, Objective, preset_specs, resolve_direction
from .params import ParamDef, ParamSpec

MANIFEST_FILENAMES = ("algops.yaml", "algops.yml")
CONTRACT_VERSION = 2
ID_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")


def _check_id(v: str, what: str) -> str:
    if not ID_PATTERN.match(v):
        raise ValueError(f"invalid {what} id {v!r}: must match {ID_PATTERN.pattern}")
    return v


class EnvSpec(BaseModel):
    """How the runner prepares a checkout before its first run (cached per commit+setup)."""

    setup: list[str] = Field(default_factory=list)  # shell commands, run in the repo root
    vars: dict[str, str] = Field(default_factory=dict)
    python: str | None = None  # interpreter for `python:` entrypoints; default = runner's own
    setup_timeout_s: float = 1800.0


class EntrypointSpec(BaseModel):
    """Exactly one of ``command`` (any language) or ``python`` (SDK shim) must be given.

    ``command`` may be an argv list or a shell string; both support placeholders:
    ``{request} {result} {output_dir} {params} {suite_path} {suite_uri} {run_id} {repo_root}``
    and ``{param:<key>}``.
    """

    command: list[str] | str | None = None
    python: str | None = None  # "path/to/file.py:func" or "package.module:func"
    cwd: str = "."
    env: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _one(self) -> "EntrypointSpec":
        if (self.command is None) == (self.python is None):
            raise ValueError("entrypoint needs exactly one of 'command' or 'python'")
        if self.python is not None and ":" not in self.python:
            raise ValueError("python entrypoint must be 'file.py:func' or 'module:func'")
        return self


class SuiteSpec(BaseModel):
    id: str
    uri: str = ""  # repo-relative path, absolute path, "${VAR}/x", or scheme://...
    description: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v, "suite")


class MetricsDecl(BaseModel):
    presets: list[str] = Field(default_factory=list)
    declare: list[MetricSpec] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _shorthand(cls, data: Any) -> Any:
        # `metrics: binary` | `metrics: [binary, {name: x, direction: minimize}]`
        if isinstance(data, str):
            return {"presets": [data]}
        if isinstance(data, list):
            presets = [d for d in data if isinstance(d, str)]
            declare = [d for d in data if not isinstance(d, str)]
            return {"presets": presets, "declare": declare}
        return data

    @model_validator(mode="after")
    def _known(self) -> "MetricsDecl":
        preset_specs(self.presets)  # raises on an unknown preset
        return self

    def specs(self) -> dict[str, MetricSpec]:
        out = {s.name: s for s in preset_specs(self.presets)}
        out.update({s.name: s for s in self.declare})
        return out


class RegressionPolicy(BaseModel):
    """How a benchmark cell is compared against its baseline.

    A metric regresses when it moves in the *worse* direction by more than
    ``max(abs_tolerance[metric], rel_tolerance * |baseline|)``.
    """

    metrics: list[str] | None = None  # None => objective metric + gate metrics
    rel_tolerance: float = 0.02
    abs_tolerance: dict[str, float] = Field(default_factory=dict)


class ObjectiveDecl(BaseModel):
    metric: str
    direction: GoalDirection | None = None  # resolved from declared metrics when omitted
    target: float | None = None
    constraints: list[Constraint] = Field(default_factory=list)

    def resolve(self, declared: dict[str, MetricSpec]) -> Objective:
        return Objective(
            metric=self.metric,
            direction=self.direction or resolve_direction(self.metric, declared),
            target=self.target,
            constraints=list(self.constraints),
        )


class AlgorithmSpec(BaseModel):
    id: str
    name: str | None = None
    description: str | None = None
    modality: str = "tabular"
    params: str | list[ParamDef] | None = None  # path (repo-relative) or inline list
    entrypoint: EntrypointSpec
    metrics: MetricsDecl = Field(default_factory=MetricsDecl)
    gates: list[Constraint] = Field(default_factory=list)
    regression: RegressionPolicy = Field(default_factory=RegressionPolicy)
    objective: ObjectiveDecl | None = None
    suites: list[str] | None = None  # None => every suite in the manifest
    runs_on: list[str] = Field(default_factory=list)  # runner labels required
    timeout_s: float = 900.0
    # Filled by the loader from `params` (path or inline) — what the master/agents consume.
    param_spec: ParamSpec | None = None

    @field_validator("id")
    @classmethod
    def _id(cls, v: str) -> str:
        return _check_id(v, "algorithm")

    def metric_specs(self) -> dict[str, MetricSpec]:
        return self.metrics.specs()

    def spec(self) -> ParamSpec:
        return self.param_spec or ParamSpec(modality=self.modality, params=[])

    def default_objective(self) -> Objective | None:
        declared = self.metric_specs()
        if self.objective is not None:
            return self.objective.resolve(declared)
        if self.gates:
            g = self.gates[0]
            return Objective(metric=g.metric, direction=resolve_direction(g.metric, declared),
                             target=g.value, constraints=list(self.gates[1:]))
        if "f1" in declared:
            return Objective(metric="f1", direction=GoalDirection.maximize)
        if declared:
            first = next(iter(declared.values()))
            return Objective(metric=first.name, direction=first.direction)
        return None

    def regression_metrics(self) -> list[str]:
        if self.regression.metrics is not None:
            return list(self.regression.metrics)
        names: list[str] = []
        obj = self.default_objective()
        if obj is not None:
            names.append(obj.metric)
            names += [c.metric for c in obj.constraints]
        names += [g.metric for g in self.gates]
        seen: list[str] = []
        for n in names:
            if n not in seen:
                seen.append(n)
        return seen


class RepoManifest(BaseModel):
    algops: int = CONTRACT_VERSION
    name: str | None = None
    env: EnvSpec = Field(default_factory=EnvSpec)
    suites: list[SuiteSpec] = Field(default_factory=list)
    algorithms: list[AlgorithmSpec]
    legacy: bool = False  # synthesized from a v1 layout

    @model_validator(mode="after")
    def _consistent(self) -> "RepoManifest":
        if self.algops not in (1, 2):
            raise ValueError(f"unsupported contract version algops={self.algops}")
        if not self.algorithms:
            raise ValueError("manifest declares no algorithms")
        if not self.suites:
            self.suites = [SuiteSpec(id="default", uri="")]
        suite_ids = [s.id for s in self.suites]
        if len(set(suite_ids)) != len(suite_ids):
            raise ValueError(f"duplicate suite ids: {suite_ids}")
        algo_ids = [a.id for a in self.algorithms]
        if len(set(algo_ids)) != len(algo_ids):
            raise ValueError(f"duplicate algorithm ids: {algo_ids}")
        for a in self.algorithms:
            for sid in a.suites or []:
                if sid not in suite_ids:
                    raise ValueError(f"algorithm {a.id!r} references unknown suite {sid!r}")
        return self

    def algorithm(self, algorithm_id: str) -> AlgorithmSpec:
        for a in self.algorithms:
            if a.id == algorithm_id:
                return a
        raise KeyError(f"unknown algorithm {algorithm_id!r} (have: {[a.id for a in self.algorithms]})")

    def suite(self, suite_id: str) -> SuiteSpec:
        for s in self.suites:
            if s.id == suite_id:
                return s
        raise KeyError(f"unknown suite {suite_id!r} (have: {[s.id for s in self.suites]})")

    def suites_for(self, algorithm_id: str) -> list[SuiteSpec]:
        algo = self.algorithm(algorithm_id)
        if algo.suites is None:
            return list(self.suites)
        return [self.suite(sid) for sid in algo.suites]

    def cells(self, algorithms: list[str] | None = None, suites: list[str] | None = None,
              tags: list[str] | None = None) -> list[tuple[str, str]]:
        """Every (algorithm_id, suite_id) pair a benchmark should evaluate, after filtering."""
        out: list[tuple[str, str]] = []
        for a in self.algorithms:
            if algorithms and a.id not in algorithms:
                continue
            for s in self.suites_for(a.id):
                if suites and s.id not in suites:
                    continue
                if tags and not set(tags) & set(s.tags):
                    continue
                out.append((a.id, s.id))
        return out


# --------------------------------------------------------------------------- loading


class ManifestError(ValueError):
    pass


def _read_yaml(path: str) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def load_params_file(path: str, modality: str = "tabular") -> ParamSpec:
    """Parse a ``params.yaml`` (``meta`` + ``params``) into a validated ParamSpec."""
    raw = _read_yaml(path)
    if isinstance(raw, list):  # bare list of params
        return ParamSpec(modality=modality, params=raw)
    meta = raw.get("meta", {}) or {}
    return ParamSpec(
        version=int(meta.get("param_spec_version", 1)),
        modality=str(meta.get("modality", modality)),
        params=raw.get("params", []) or [],
    )


def find_manifest(repo_root: str) -> str | None:
    for name in MANIFEST_FILENAMES:
        path = os.path.join(repo_root, name)
        if os.path.isfile(path):
            return path
    return None


def _legacy_manifest(repo_root: str) -> dict | None:
    params = os.path.join(repo_root, "algops", "params.yaml")
    adapter = os.path.join(repo_root, "algops", "run_adapter.py")
    if not (os.path.isfile(params) and os.path.isfile(adapter)):
        return None
    modality = "tabular"
    try:
        modality = str((_read_yaml(params).get("meta") or {}).get("modality", "tabular"))
    except Exception:  # noqa: BLE001
        pass
    return {
        "algops": 1,
        "legacy": True,
        "algorithms": [{
            "id": "default",
            "modality": modality,
            "params": "algops/params.yaml",
            "entrypoint": {"python": "algops/run_adapter.py:run"},
            "metrics": {"presets": ["binary"]},
        }],
        "suites": [{"id": "default", "uri": ""}],
    }


def parse_manifest(raw: dict, repo_root: str | None = None) -> RepoManifest:
    """Validate a raw manifest dict and resolve every algorithm's parameter spec."""
    try:
        manifest = RepoManifest.model_validate(raw)
    except Exception as exc:  # noqa: BLE001
        raise ManifestError(f"invalid algops manifest: {exc}") from exc
    for algo in manifest.algorithms:
        if algo.param_spec is not None and algo.params is None:
            continue  # already resolved (e.g. received from a runner over the wire)
        if algo.params is None:
            algo.param_spec = ParamSpec(modality=algo.modality, params=[])
        elif isinstance(algo.params, list):
            algo.param_spec = ParamSpec(modality=algo.modality, params=algo.params)
        else:
            if repo_root is None:
                if algo.param_spec is None:
                    raise ManifestError(f"{algo.id}: params path needs a repo root to resolve")
                continue
            path = os.path.join(repo_root, algo.params)
            if not os.path.isfile(path):
                raise ManifestError(f"{algo.id}: params file not found: {algo.params}")
            try:
                algo.param_spec = load_params_file(path, algo.modality)
            except Exception as exc:  # noqa: BLE001
                raise ManifestError(f"{algo.id}: invalid params file {algo.params}: {exc}") from exc
    return manifest


def load_manifest(repo_root: str) -> RepoManifest:
    """Load ``<repo_root>/algops.yaml`` (or a synthesized legacy v1 manifest)."""
    path = find_manifest(repo_root)
    if path is not None:
        raw = _read_yaml(path)
        if not isinstance(raw, dict):
            raise ManifestError(f"{path}: top level must be a mapping")
    else:
        raw = _legacy_manifest(repo_root)
        if raw is None:
            raise ManifestError(
                f"no algops.yaml (or legacy algops/params.yaml + run_adapter.py) in {repo_root}"
            )
    return parse_manifest(raw, repo_root)


def manifest_to_wire(manifest: RepoManifest) -> dict:
    """JSON-safe manifest with resolved param specs (what a runner sends the master)."""
    return manifest.model_dump(mode="json")


def manifest_from_wire(data: dict) -> RepoManifest:
    return RepoManifest.model_validate(data)
