"""Generic params.yaml loading for externalized target projects (plan §2A).

Decoupled from any sibling module so a generated ``run_adapter.py`` can load parameters without
importing a project-local ``config.py`` — which keeps the verify gate able to import the adapter
standalone. ``effective_params`` validates against the ALGOps ParamSpec, then applies + clamps
the run's overrides.
"""

from __future__ import annotations

import os
from typing import Any

import yaml

from algops.core import ParamSpec
from algops.core.enums import Dtype


def load_param_spec(algops_dir: str) -> ParamSpec:
    """Parse + validate ``<algops_dir>/params.yaml`` into a ParamSpec (raises if invalid)."""
    path = os.path.join(algops_dir, "params.yaml")
    raw = yaml.safe_load(open(path, encoding="utf-8")) or {}
    meta = raw.get("meta", {})
    return ParamSpec(
        version=int(meta.get("param_spec_version", 1)),
        modality=str(meta.get("modality", "tabular")),
        params=raw.get("params", []),
    )


def effective_params(algops_dir: str, overrides: dict[str, Any] | None = None) -> dict[str, Any]:
    """Validated defaults with the run's overrides applied (and clamped to each param's range)."""
    spec = load_param_spec(algops_dir)
    values = spec.defaults()
    by_key = spec.by_key()
    for key, value in (overrides or {}).items():
        pdef = by_key.get(key)
        if pdef is not None and pdef.dtype in (Dtype.float, Dtype.int):
            values[key] = pdef.clamp(value)
        else:
            values[key] = value
    return values
