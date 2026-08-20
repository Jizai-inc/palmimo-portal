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
# The frontend's build output (frontend/ -> `make build` -> here), committed as a build
# artifact -- see app.py's `_mount_frontend`. Resolved from this module's own location, not
# an environment variable: not operator-configurable, only a test seam (`static_dir` field).
DEFAULT_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Climbs from this file past the `palmimo_portal` package dir to the repository root --
# the Portal checkout this process is itself running out of, and what GitUvUpdater updates.
DEFAULT_PORTAL_DIR = Path(__file__).resolve().parents[1]
DEFAULT_UPDATE_REPO = "Jizai-inc/palmimo-portal"
DEFAULT_PORTAL_UNIT = "palmimo-portal.service"
DEFAULT_UV_BIN = "uv"

AdapterMode = Literal["fake", "real"]


@dataclass(frozen=True)
class Settings:
    """Resolved Portal configuration."""

    state_dir: Path = DEFAULT_STATE_DIR  #: JsonFileStateStore's auth.json / last_attempt.json dir
    port: int = DEFAULT_PORT  #: uvicorn bind port (__main__)
    #: FileIdentityStore's manufacturing-identity file. Read-only for the Portal; absence is
    #: a supported state (a DIY, self-flashed image) -- see ports.IdentityStore.
    identity_file: Path = DEFAULT_IDENTITY_FILE
    #: Extra Host header values HostGuard accepts, beyond hostname/<hostname>.local/IPs/localhost.
    allowed_hosts: frozenset[str] = field(default_factory=frozenset)
    #: Which adapter set create_app wires up: "fake" (default) uses the in-memory
    #: testing.fakes adapters; "real" uses the OS-backed ones (comitup/logind over D-Bus,
    #: filesystem for the rest). See wiring.build_adapters.
    adapters: AdapterMode = "fake"
    #: Serve FastAPI's /docs, /redoc, /openapi.json. False by default -- those routes are
    #: unauthenticated and would expose the whole admin API schema to anyone on the LAN.
    #: Env PALMIMO_ENABLE_DOCS.
    enable_docs: bool = False
    #: Frontend build output dir (app.py's _mount_frontend). Not read from the environment
    #: (see DEFAULT_STATIC_DIR) -- a test seam so tests can point it at an empty/missing dir.
    static_dir: Path = DEFAULT_STATIC_DIR
    #: Portal checkout GitUvUpdater applies fetch/checkout/uv sync to. Defaults to the checkout
    #: this process runs out of; overridable via PALMIMO_PORTAL_DIR mainly for tests.
    portal_dir: Path = DEFAULT_PORTAL_DIR
    update_repo: str = DEFAULT_UPDATE_REPO  #: owner/repo GitHubReleaseSource queries. Env PALMIMO_UPDATE_REPO
    #: systemd unit SystemdSystemPort.restart_portal restarts after an update applies.
    #: Env PALMIMO_PORTAL_UNIT.
    portal_unit: str = DEFAULT_PORTAL_UNIT
    #: uv executable GitUvUpdater runs sync with. Not necessarily resolvable via a bare "uv" on
    #: PATH under systemd -- see GitUvUpdater._resolve_uv_bin's fallback. Env PALMIMO_UV_BIN.
    uv_bin: str = DEFAULT_UV_BIN
    #: Whether UpdateRunner runs an apply/rollback job on a background thread (the real
    #: default) or inline before POST /update/apply|rollback returns. A test seam, not read
    #: from the environment, so tests can drive the fake updater synchronously.
    update_run_in_thread: bool = True
    #: How long UpdateRunner sleeps after persisting "restarting" and before calling
    #: SystemPort.restart_portal -- long enough for the HTTP response that triggered the
    #: restart to finish flushing before systemd kills this process. Tests set this to 0.
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
