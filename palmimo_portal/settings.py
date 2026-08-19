"""Environment-driven configuration for the Palmimo Portal.

Deliberately not a pydantic-settings model: the allowed-dependency list for
this PR is ``fastapi``, ``uvicorn``, ``argon2-cffi``, and ``itsdangerous``
(plus ``httpx`` for tests), so configuration is read from ``os.environ`` by
hand instead of adding another dependency for it.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal


DEFAULT_STATE_DIR = Path("/var/lib/palmimo/portal")
DEFAULT_PORT = 8080
DEFAULT_IDENTITY_FILE = Path("/boot/firmware/palmimo-identity.json")
# The frontend's build output (frontend/ -> `make
# build` -> here), committed as a build artifact -- see app.py's
# `_mount_frontend`. Resolved from this module's own location, not an
# environment variable: not an operator-configurable deployment path, only
# a test seam (see the `static_dir` field below).
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"

# The Portal repository checkout this process is itself running out of:
# climbs from this file past the `palmimo_portal` package dir to the
# repository root. See Settings.portal_dir's docstring for why this is what
# GitUvUpdater updates.
DEFAULT_PORTAL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_UPDATE_REPO = "Jizai-inc/palmimo-portal"
DEFAULT_PORTAL_UNIT = "palmimo-portal.service"
DEFAULT_UV_BIN = "uv"

AdapterMode = Literal["fake", "real"]


@dataclass(frozen=True)
class Settings:
    """Resolved Portal configuration.

    Attributes:
        state_dir: Where :class:`~palmimo_portal.adapters.state.JsonFileStateStore`
            persists ``auth.json`` and ``last_attempt.json``.
        port: The port :mod:`palmimo_portal.__main__` binds uvicorn to.
        identity_file: Where :class:`~palmimo_portal.adapters.identity.FileIdentityStore`
            reads the manufacturing-written identity file. Read-only for
            the Portal; absence is a supported state (a DIY, self-flashed
            image) -- see :mod:`palmimo_portal.ports`'s ``IdentityStore``.
        allowed_hosts: Extra ``Host`` header values HostGuard accepts, beyond
            the machine's own hostname, ``<hostname>.local``, its IP
            addresses, and ``localhost``.
        adapters: Which adapter set :func:`palmimo_portal.api.app.create_app`
            wires up. ``"fake"`` (the default) uses the in-memory adapters in
            :mod:`palmimo_portal.testing.fakes` for every port. ``"real"``
            uses the OS-backed adapters for every port -- comitup and logind
            over D-Bus for the network and system ports, the filesystem for
            the rest. See :func:`palmimo_portal.wiring.build_adapters`.
        enable_docs: Whether to serve FastAPI's ``/docs``, ``/redoc``, and
            ``/openapi.json``. ``False`` by default -- those routes are
            unauthenticated and would expose the whole admin API schema to
            anyone on the LAN. Set ``PALMIMO_ENABLE_DOCS`` truthy for local
            development.
        static_dir: Where :func:`palmimo_portal.api.app._mount_frontend` looks
            for the frontend's build output. Not read from the environment
            (see :data:`DEFAULT_STATIC_DIR`); this field is a test seam so
            tests can point it at an empty or nonexistent directory.
        portal_dir: The Portal repository checkout
            :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`
            applies an update to -- ``git fetch``/``checkout``/``uv sync``
            all run against this directory. Defaults to the checkout this
            process runs out of (:data:`DEFAULT_PORTAL_DIR`); overridable
            via ``PALMIMO_PORTAL_DIR`` mainly for tests.
        update_repo: The ``owner/repo`` GitHub queries for releases --
            :class:`~palmimo_portal.adapters.github_releases.GitHubReleaseSource`.
            Env ``PALMIMO_UPDATE_REPO``.
        portal_unit: The systemd unit
            :class:`~palmimo_portal.adapters.systemd.SystemdSystemPort.restart_portal`
            restarts once an update finishes applying. Env
            ``PALMIMO_PORTAL_UNIT``.
        uv_bin: The ``uv`` executable :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`
            runs ``sync`` with. Not necessarily resolvable via a bare
            ``"uv"`` on ``PATH`` under systemd -- see
            :meth:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater._resolve_uv_bin`
            for the ``shutil.which``/``~/.local/bin/uv`` fallback. Env
            ``PALMIMO_UV_BIN``.
        update_run_in_thread: Whether
            :class:`~palmimo_portal.core.update_runner.UpdateRunner` runs an
            apply/rollback job on a background thread (the real default) or
            inline before ``POST /update/apply``/``POST /update/rollback``
            returns. A test seam, not read from the environment, so tests
            can drive the fake updater synchronously.
        update_restart_delay_seconds: How long
            :class:`~palmimo_portal.core.update_runner.UpdateRunner` sleeps
            after persisting ``"restarting"`` and before calling
            :meth:`~palmimo_portal.ports.SystemPort.restart_portal` -- long
            enough for the HTTP response that triggered the restart to
            finish flushing before systemd kills this process. A test seam
            like ``update_run_in_thread``: tests set this to ``0``.
    """

    state_dir: Path = DEFAULT_STATE_DIR
    port: int = DEFAULT_PORT
    identity_file: Path = DEFAULT_IDENTITY_FILE
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    adapters: AdapterMode = "fake"
    enable_docs: bool = False
    static_dir: Path = DEFAULT_STATIC_DIR
    portal_dir: Path = DEFAULT_PORTAL_DIR
    update_repo: str = DEFAULT_UPDATE_REPO
    portal_unit: str = DEFAULT_PORTAL_UNIT
    uv_bin: str = DEFAULT_UV_BIN
    update_run_in_thread: bool = True
    update_restart_delay_seconds: float = 1.0


_TRUTHY_ENV_VALUES = frozenset({"1", "true", "yes", "on"})


def _env_flag(name: str) -> bool:
    """Report whether the environment variable ``name`` is set to a truthy value."""
    return os.environ.get(name, "").strip().lower() in _TRUTHY_ENV_VALUES


def get_settings() -> Settings:
    """Read :class:`Settings` from the process environment."""
    state_dir = Path(os.environ.get("PALMIMO_STATE_DIR", str(DEFAULT_STATE_DIR)))
    identity_file = Path(os.environ.get("PALMIMO_IDENTITY_FILE", str(DEFAULT_IDENTITY_FILE)))
    port_raw = os.environ.get("PALMIMO_PORT", str(DEFAULT_PORT))
    try:
        port = int(port_raw)
    except ValueError as error:
        raise ValueError(f"PALMIMO_PORT must be an integer, got {port_raw!r}") from error
    allowed_hosts_raw = os.environ.get("PALMIMO_ALLOWED_HOSTS", "")
    allowed_hosts = frozenset(host.strip() for host in allowed_hosts_raw.split(",") if host.strip())
    adapters_raw = os.environ.get("PALMIMO_ADAPTERS", "fake").strip().lower()
    if adapters_raw not in ("fake", "real"):
        raise ValueError(f"PALMIMO_ADAPTERS must be 'fake' or 'real', got {adapters_raw!r}")
    adapters: AdapterMode = adapters_raw  # type: ignore[assignment]
    enable_docs = _env_flag("PALMIMO_ENABLE_DOCS")
    portal_dir = Path(os.environ.get("PALMIMO_PORTAL_DIR", str(DEFAULT_PORTAL_DIR)))
    update_repo = os.environ.get("PALMIMO_UPDATE_REPO", DEFAULT_UPDATE_REPO)
    portal_unit = os.environ.get("PALMIMO_PORTAL_UNIT", DEFAULT_PORTAL_UNIT)
    uv_bin = os.environ.get("PALMIMO_UV_BIN", DEFAULT_UV_BIN)
    return Settings(
        state_dir=state_dir,
        port=port,
        identity_file=identity_file,
        allowed_hosts=allowed_hosts,
        adapters=adapters,
        enable_docs=enable_docs,
        portal_dir=portal_dir,
        update_repo=update_repo,
        portal_unit=portal_unit,
        uv_bin=uv_bin,
    )
