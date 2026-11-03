"""CLI: ``uv run python -m palmimo_portal`` -- serve the Portal over uvicorn.

Binds ``0.0.0.0`` (every interface — the real device serves during AP mode,
before any DHCP-assigned address exists to bind to specifically) on the port
``PALMIMO_PORT`` names, defaulting to 8080.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from palmimo_portal.api.app import create_app
from palmimo_portal.settings import Settings, get_settings


def _configure_logging() -> None:
    """Configure the root logger from ``PALMIMO_LOG_LEVEL`` (default ``INFO``).

    Called only here, never at import time -- importing :mod:`palmimo_portal`
    as a library must not reconfigure the importer's own logging setup.
    """
    level_name = os.environ.get("PALMIMO_LOG_LEVEL", "INFO").strip().upper()
    level = logging.getLevelNamesMapping().get(level_name, logging.INFO)
    # force=True: another importer (e.g. pytest's logging plugin) may already
    # have attached a handler to the root logger, which would otherwise make
    # basicConfig a silent no-op.
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)


def server_config(settings: Settings) -> uvicorn.Config:
    # Portal listens directly; no upstream proxy is trusted to supply peer addresses.
    return uvicorn.Config(create_app(settings), host="0.0.0.0", port=settings.port, proxy_headers=False)


def main() -> None:
    """Configure logging, build the app from environment settings, and serve it."""
    _configure_logging()
    uvicorn.Server(server_config(get_settings())).run()


if __name__ == "__main__":
    main()
