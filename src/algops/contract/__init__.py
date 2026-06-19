"""algops.contract — the parameter-externalization contract: bundle, introspection, verify gate (M4)."""

from __future__ import annotations

from .introspect import scan_candidates, write_understanding
from .params import effective_params, load_param_spec
from .verify import GateResult, run_gate, run_gate_package, scan_hardcoded

__all__ = [
    "effective_params",
    "load_param_spec",
    "GateResult",
    "run_gate",
    "run_gate_package",
    "scan_hardcoded",
    "scan_candidates",
    "write_understanding",
]


def materialize_bundle(target_root: str):
    from .cli import materialize_bundle as _m

    return _m(target_root)
