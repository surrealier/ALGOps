"""algops.sdk — the Python SDK for algorithm entrypoints (re-exports the stdlib-only module).

Repositories evaluated by a runner can ``import algops_sdk`` without installing ALGOps (the
runner injects the file); environments that do have ALGOps can ``from algops import sdk``.
"""

from __future__ import annotations

import os

from .algops_sdk import (  # noqa: F401
    JOIN_COLUMNS,
    PROTOCOL_REQUEST,
    PROTOCOL_RESULT,
    RESULT_MARKER,
    Context,
    StageWriter,
    counts,
    failed,
    main,
    read_request,
    result,
    run_target,
    write_result,
)

SDK_FILE = os.path.join(os.path.dirname(__file__), "algops_sdk.py")

__all__ = [
    "JOIN_COLUMNS", "PROTOCOL_REQUEST", "PROTOCOL_RESULT", "RESULT_MARKER",
    "Context", "StageWriter", "counts", "failed", "main", "read_request", "result",
    "run_target", "write_result", "SDK_FILE",
]
