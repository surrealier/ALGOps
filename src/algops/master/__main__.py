"""Run the master API server: ``uv run algops-master`` (or ``python -m algops.master``)."""

from __future__ import annotations

import uvicorn

from algops.core import MasterSettings

from .app import create_app


def main() -> None:
    settings = MasterSettings()
    app = create_app(settings)
    uvicorn.run(app, host=settings.host, port=settings.port, log_level="info")


if __name__ == "__main__":
    main()
