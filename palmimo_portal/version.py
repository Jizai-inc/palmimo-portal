"""Resolves the installed ``palmimo-portal`` distribution version.

Shared by :mod:`palmimo_portal.api.system` (the ``/system/status`` response)
and :mod:`palmimo_portal.api.app` (the startup banner) so there is exactly one
place that knows how to ask ``importlib.metadata`` for it.
"""

from __future__ import annotations

from importlib.metadata import PackageNotFoundError, version


def package_version(name: str) -> str | None:
    """Return an installed distribution's version, or ``None`` if it is not importable."""
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def portal_version() -> str:
    """Return the installed ``palmimo-portal`` version, or ``"0.0.0"`` if unresolvable."""
    return package_version("palmimo-portal") or "0.0.0"
