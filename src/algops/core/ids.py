"""Canonical, time-ordered identifiers (UUIDv7).

The whole system hangs off a small ID hierarchy:

    project_id  ->  experiment_id (campaign == one Run-button press == LangGraph thread)
                ->  run_id        (one param-set evaluation over the test set)
                ->  iteration_index (int loop step that produced the run)

`run_id` is the *integrity spine*: minted by the master, passed verbatim to the slave
run-adapter, and (from M3) written identically into every CSV row, Qdrant payload, and
Neo4j ``:Run`` node. UUIDv7 is time-ordered so ids sort by creation — handy for history.
"""

from __future__ import annotations

import os
import time
import uuid


def uuid7() -> uuid.UUID:
    """Generate a UUIDv7 (48-bit ms timestamp + version/variant + randomness)."""
    ms = (time.time_ns() // 1_000_000) & ((1 << 48) - 1)
    rand_a = int.from_bytes(os.urandom(2), "big") & 0x0FFF  # 12 bits
    rand_b = int.from_bytes(os.urandom(8), "big") & ((1 << 62) - 1)  # 62 bits

    value = ms << 80
    value |= 0x7 << 76  # version 7
    value |= rand_a << 64
    value |= 0b10 << 62  # RFC 4122 variant
    value |= rand_b
    return uuid.UUID(int=value)


def new_id() -> str:
    """A fresh UUIDv7 as a canonical string (the form stored everywhere)."""
    return str(uuid7())


def new_token() -> str:
    """An opaque bearer/enroll token (not an id; never time-ordered)."""
    return uuid.uuid4().hex + uuid.uuid4().hex
