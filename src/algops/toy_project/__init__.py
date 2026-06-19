"""algops.toy_project — bundled hand-externalized synthetic detector.

Serves two purposes: (1) M0's evaluable target so the loop closes end-to-end without a real
project; (2) the golden reference for the M4 coding-agent externalization flow — its
params.yaml / config.py / run_adapter.py are exactly the shape that flow must generate.
"""

from __future__ import annotations

from .config import load_param_spec, load_params
from .run_adapter import run

__all__ = ["load_params", "load_param_spec", "run"]
