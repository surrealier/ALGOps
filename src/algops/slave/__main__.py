"""Run the slave agent: ``uv run algops-slave`` (or ``python -m algops.slave``)."""

from __future__ import annotations

import asyncio

from algops.core import SlaveSettings

from .poller import run_slave


def main() -> None:
    import sys

    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    asyncio.run(run_slave(SlaveSettings()))


if __name__ == "__main__":
    main()
