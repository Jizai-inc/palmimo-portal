"""FastAPI assembly: the middleware chain, router registration, and adapter wiring.

Middleware runs outside-in as **HostGuard -> Session -> CSRF -> router**:

- :class:`HostGuardMiddleware` rejects DNS-rebinding-style requests (a
  ``Host`` header that names neither this machine nor an explicitly allowed
  one) with 421, before anything else runs -- except an OS captive-portal
  probe while unprovisioned, which it redirects to this machine so the
  setup AP's captive portal opens automatically.
- :class:`SessionMiddleware` reads the session cookie, if any, and records
  whether it verifies against the current signing key on
  ``request.state.authenticated`` — it never itself rejects a request,
  since many endpoints are intentionally reachable without a session.
  Enforcement is per-route, via :func:`~palmimo_portal.api.deps.require_auth` /
  :func:`~palmimo_portal.api.deps.require_wifi_access`.
- :class:`CSRFMiddleware` requires the ``X-Requested-With: PalmimoPortal``
  header on every state-changing request — a header a cross-site form
  submission cannot attach.

Every error response — including FastAPI's own request-validation errors —
is translated to the Portal's ``{"error": {"code", "params"}}`` envelope by
the exception handlers registered in :func:`create_app`.
"""

from __future__ import annotations

import asyncio
import logging
import socket
import struct
import threading
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import anyio.to_thread
from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint

from palmimo_portal.api import apps as apps_api
from palmimo_portal.api import auth as auth_api
from palmimo_portal.api import catalog as catalog_api
from palmimo_portal.api import platform as platform_api
from palmimo_portal.api import secrets as secrets_api
from palmimo_portal.api import ssh_keys as ssh_keys_api
from palmimo_portal.api import system as system_api
from palmimo_portal.api import update as update_api
from palmimo_portal.api import wifi as wifi_api
from palmimo_portal.core.apps import finalize_apps_state
from palmimo_portal.core.apps_job_runner import AppsJobRunner
from palmimo_portal.core.apps_jobs import (
    AppsJobContext,
    cleanup_staging_and_trash,
    disk_state_checks,
    sweep_orphan_app_dirs,
)
from palmimo_portal.core.apps_start import (
    RUNNING_ACTIVE_STATES,
    AppStartError,
    StartDeps,
    reconcile_run_dirs,
    start_app,
)
from palmimo_portal.core.auth import SESSION_COOKIE_NAME, LoginRateLimiter, ResetRateLimiter, decode_session
from palmimo_portal.core.catalog import CatalogCache
from palmimo_portal.core.periodic import build_scheduler
from palmimo_portal.core.platform import (
    PlatformJobRunner,
    PlatformLatestCache,
    PlatformUpdateRunner,
    compute_status,
    run_verify,
)
from palmimo_portal.core.platform_update import finalize_after_restart as finalize_platform_after_restart
from palmimo_portal.core.provisioning import is_provisioned
from palmimo_portal.core.update import finalize_after_restart
from palmimo_portal.core.update_runner import UpdateRunner
from palmimo_portal.ports import (
    AdapterUnavailableError,
    AppNotFoundError,
    AppsStateFileState,
    AuthFileState,
    Identity,
    IdentityStore,
    NetworkPort,
    PolkitDeniedError,
    StateStore,
)
from palmimo_portal.settings import Settings, get_settings
from palmimo_portal.version import portal_version
from palmimo_portal.wiring import AdapterBundle, build_adapters


# The prefix every API router registers under. The SPA fallback route below
# must never answer for this prefix -- an unmatched API path must keep
# 404ing as JSON, not silently serve index.html.
API_PATH_PREFIX = "api/"


logger = logging.getLogger("palmimo_portal")

STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
CSRF_HEADER_NAME = "x-requested-with"
CSRF_HEADER_VALUE = "PalmimoPortal"

# How long HostGuard trusts its cached snapshot of this machine's own
# hostnames/IPs before re-resolving. Short enough that an address which
# appears after boot (e.g. DHCP finally assigning one once the device
# leaves AP mode) becomes allowed without a restart.
_HOST_CACHE_TTL_SECONDS = 30.0

# How long HostGuard trusts its cached provisioned/unprovisioned snapshot
# before re-asking the network port. Short enough that finishing Wi-Fi setup
# stops the captive-probe redirect within one probe interval, without
# hammering D-Bus on every probe (OSes poll this every few seconds).
_PROVISIONED_CACHE_TTL_SECONDS = 5.0

# Hostnames the major OSes' connectivity-check clients query to detect a
# captive portal. Wildcard DNS on the setup AP (comitup's dnsmasq) answers
# every one of these with the AP's own IP, routing the probe here.
CAPTIVE_PROBE_HOSTS = frozenset(
    {
        "captive.apple.com",
        "connectivitycheck.gstatic.com",
        "connectivitycheck.android.com",
        "clients3.google.com",
        "www.msftconnecttest.com",
        "msftconnecttest.com",
        "www.msftncsi.com",
        "msftncsi.com",
        "detectportal.firefox.com",
        "nmcheck.gnome.org",
    }
)

_CAPTIVE_PROBE_METHODS = frozenset({"GET", "HEAD"})


def _is_api_path(path: str) -> bool:
    """Match the SPA fallback's own API-namespace check (see ``_serve_spa``)."""
    return path.startswith(f"/{API_PATH_PREFIX}") or path == f"/{API_PATH_PREFIX.rstrip('/')}"


_SIOCGIFADDR = 0x8915


def _interface_ipv4_addresses() -> frozenset[str]:
    """Enumerate this machine's own bound interface IPv4 addresses (Linux only).

    ``gethostbyname_ex`` -- the other source in ``_machine_hosts`` -- resolves
    through the resolver, not the network stack, so on a Pi it typically only
    finds the ``/etc/hosts`` loopback alias, missing the AP-mode gateway IP a
    setup client would connect to by numeric IP (which HostGuard would then
    reject with 421). ``SIOCGIFADDR`` reads what the kernel actually has bound
    to each interface, closing that gap. Linux-specific; any failure is
    swallowed as "nothing found here", falling back to the other sources.
    """
    addresses: set[str] = set()
    try:
        import fcntl  # POSIX-only; lazy import so this module still loads elsewhere

        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            for _, name in socket.if_nameindex():
                try:
                    packed = fcntl.ioctl(
                        probe.fileno(),
                        _SIOCGIFADDR,
                        struct.pack("256s", name[:15].encode("ascii")),
                    )
                except OSError:
                    continue  # no IPv4 bound (down, or IPv6-only)
                addresses.add(socket.inet_ntoa(packed[20:24]))
    except (ImportError, AttributeError, OSError):
        return frozenset()
    return frozenset(addresses)


def _machine_hosts() -> frozenset[str]:
    """Return this machine's own hostname, ``<hostname>.local``, and IP addresses."""
    hostname = socket.gethostname()
    hosts = {hostname, f"{hostname}.local", "localhost", "127.0.0.1"}
    try:
        _, _, addresses = socket.gethostbyname_ex(hostname)
        hosts.update(addresses)
    except OSError:
        pass  # no resolvable address yet (e.g. AP mode, no DHCP lease)
    hosts.update(_interface_ipv4_addresses())
    return frozenset(hosts)


def _normalize_host(host_header: str) -> str:
    """Strip a ``:port`` suffix from a ``Host`` header value, IPv6 literals included."""
    if host_header.startswith("["):
        # IPv6 literal, e.g. "[::1]:8080" -> "::1".
        return host_header[1:].split("]", 1)[0]
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


class HostGuardMiddleware(BaseHTTPMiddleware):
    """Rejects any request whose ``Host`` header does not name this machine.

    A DNS-rebinding defense: a browser tab on an attacker's page can be made
    to issue a request that resolves to this device's LAN IP, carrying the
    visitor's cookies, without this check. ``always_allowed_hosts``
    (localhost + ``settings.allowed_hosts``) is fixed at construction; this
    machine's own hostname/IPs re-resolve lazily, at most once per
    :data:`_HOST_CACHE_TTL_SECONDS`, rather than once at :func:`create_app`
    time -- resolving before the network is up would otherwise permanently
    exclude an address that only becomes valid after boot.

    One exception to the 421: a GET/HEAD to a non-API path (see
    :func:`_is_api_path`) with a :data:`CAPTIVE_PROBE_HOSTS` ``Host``
    header, while the device is unprovisioned, gets a 302 to this machine
    instead -- see :meth:`dispatch`.
    """

    def __init__(
        self,
        app: Any,
        always_allowed_hosts: frozenset[str],
        network: NetworkPort | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(app)
        self._always_allowed_hosts = always_allowed_hosts
        self._network = network
        self._clock = clock
        self._cached_machine_hosts: frozenset[str] = frozenset()
        self._cached_at: float = float("-inf")
        self._cached_provisioned: bool = True
        self._provisioned_cached_at: float = float("-inf")
        self._provisioned_warned_at: float = float("-inf")
        self._provisioned_refresh_lock = asyncio.Lock()

    def _allowed_hosts(self) -> frozenset[str]:
        now = self._clock()
        if now - self._cached_at > _HOST_CACHE_TTL_SECONDS:
            self._cached_machine_hosts = _machine_hosts()
            self._cached_at = now
        return self._always_allowed_hosts | self._cached_machine_hosts

    async def _is_provisioned(self) -> bool:
        """Report provisioned state, cached for :data:`_PROVISIONED_CACHE_TTL_SECONDS`.

        The port call runs off the event loop (:func:`anyio.to_thread.run_sync`)
        -- the real adapter's D-Bus round trip can take several seconds, and
        blocking the loop for that would stall every other request the
        moment a probe lands. A refresh past the TTL takes
        ``_provisioned_refresh_lock`` and re-checks the TTL once inside it,
        so a burst of simultaneous probes triggers one port call, not one
        per request; the rest just wait on the lock.

        Fails closed: a missing network port or a raising one is treated as
        provisioned, so the ambiguous case keeps the existing 421 behavior
        rather than opening the redirect exception. A raise is logged at
        most once per TTL window, so a broken D-Bus link during a probe
        storm does not flood the log.
        """
        if self._network is None:
            return True
        if self._clock() - self._provisioned_cached_at <= _PROVISIONED_CACHE_TTL_SECONDS:
            return self._cached_provisioned
        async with self._provisioned_refresh_lock:
            now = self._clock()
            if now - self._provisioned_cached_at <= _PROVISIONED_CACHE_TTL_SECONDS:
                return self._cached_provisioned
            network = self._network
            try:
                self._cached_provisioned = await anyio.to_thread.run_sync(is_provisioned, network)
            except Exception as error:
                if now - self._provisioned_warned_at > _PROVISIONED_CACHE_TTL_SECONDS:
                    logger.warning("HostGuard: network port unavailable, treating as provisioned: %s", error)
                    self._provisioned_warned_at = now
                self._cached_provisioned = True
            self._provisioned_cached_at = self._clock()
            return self._cached_provisioned

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Any:
        host = _normalize_host(request.headers.get("host", ""))
        if host not in self._allowed_hosts():
            if (
                host in CAPTIVE_PROBE_HOSTS
                and request.method in _CAPTIVE_PROBE_METHODS
                and not _is_api_path(request.url.path)
                and not await self._is_provisioned()
            ):
                return RedirectResponse(url=f"http://{socket.gethostname()}.local/", status_code=302)
            logger.warning("HostGuard rejected a request: Host=%r", host)
            return JSONResponse(
                status_code=421,
                content={"error": {"code": "host_not_allowed", "params": {"host": host}}},
            )
        return await call_next(request)


class SessionMiddleware(BaseHTTPMiddleware):
    """Reads the session cookie and records whether it verifies, without enforcing anything.

    Two signing keys are tried depending on ``auth.json``'s state -- never
    both, never when CORRUPT:

    - **PRESENT** (full mode): verified against ``AuthState.signing_key``.
      A password change rotates this key, invalidating every full-mode
      session at once.
    - **ABSENT** (initial mode, only reachable with an identity file):
      verified against
      :meth:`~palmimo_portal.ports.StateStore.read_or_create_initial_signing_key`.
      Changing the password from initial mode creates ``auth.json`` under a
      fresh full-mode key, so an initial-mode token stops matching either
      key the moment that happens -- no separate revocation step needed.
    - **CORRUPT**: neither key is consulted -- a cookie issued before
      corruption must not keep authenticating against untrustworthy state.

    ``request.state.session_mode`` records which kind of session (if any)
    verified -- ``"full"``, ``"initial"``, or ``None`` -- for
    :func:`~palmimo_portal.api.deps.require_full_session` to gate on.
    """

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Any:
        request.state.authenticated = False
        request.state.session_mode = None
        token = request.cookies.get(SESSION_COOKIE_NAME)
        if token:
            adapters = request.app.state.adapters
            state: StateStore = adapters.state
            identity_store: IdentityStore = adapters.identity
            auth = state.read_auth()
            if auth is not None:
                payload = decode_session(auth.signing_key, token)
                if payload is not None:
                    request.state.authenticated = True
                    request.state.session_mode = payload.get("mode", "full")
            elif state.auth_state() is not AuthFileState.CORRUPT and isinstance(
                identity_store.read_identity(), Identity
            ):
                initial_key = state.read_or_create_initial_signing_key()
                payload = decode_session(initial_key, token)
                if payload is not None:
                    request.state.authenticated = True
                    request.state.session_mode = payload.get("mode", "initial")
        return await call_next(request)


class CSRFMiddleware(BaseHTTPMiddleware):
    """Requires ``X-Requested-With: PalmimoPortal`` on every state-changing request."""

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Any:
        if request.method in STATE_CHANGING_METHODS and request.headers.get(CSRF_HEADER_NAME) != CSRF_HEADER_VALUE:
            return JSONResponse(
                status_code=403,
                content={"error": {"code": "csrf_header_missing", "params": {}}},
            )
        return await call_next(request)


def _error_envelope(status_code: int, code: str, params: dict[str, Any]) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"code": code, "params": params}})


async def _handle_http_exception(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, HTTPException)
    if isinstance(exc.detail, dict) and "code" in exc.detail:
        return _error_envelope(exc.status_code, str(exc.detail["code"]), dict(exc.detail.get("params", {})))
    return _error_envelope(exc.status_code, "http_error", {"detail": str(exc.detail)})


async def _handle_validation_error(request: Request, exc: Exception) -> JSONResponse:
    assert isinstance(exc, RequestValidationError)
    return _error_envelope(422, "validation_error", {"errors": exc.errors()})


async def _handle_unexpected_error(request: Request, exc: Exception) -> JSONResponse:
    return _error_envelope(500, "internal_error", {})


async def _handle_adapter_unavailable(request: Request, exc: Exception) -> JSONResponse:
    """Translate :class:`~palmimo_portal.ports.AdapterUnavailableError` into a 503 error envelope.

    Registered app-wide, not per-endpoint: a D-Bus failure can surface from
    a dependency (``require_wifi_access``, ``require_provisioned``, etc.
    all call :meth:`~palmimo_portal.ports.NetworkPort.get_status`) before a
    gated endpoint's own body ever runs, so one handler covers every site.
    """
    assert isinstance(exc, AdapterUnavailableError)
    logger.error("adapter unavailable: %s", exc)
    return _error_envelope(503, exc.code, {})


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Resolve a ``"restarting"`` update job left over from before this process started.

    Runs on every startup: a ``"restarting"`` job persisted just before
    ``restart_portal()`` (:class:`~palmimo_portal.core.update_runner.UpdateRunner`)
    is only observed once the *new* process asks
    :meth:`~palmimo_portal.ports.Updater.installed` what tag it landed on --
    see :func:`~palmimo_portal.core.update.finalize_after_restart` for the
    done/failed decision. A no-op, safe unconditionally, on every other boot.
    """
    adapters: AdapterBundle = app.state.adapters
    try:
        state = adapters.state.read_update_state()
        finalized = finalize_after_restart(state, adapters.updater.installed(), time.time())
        if finalized is not state:
            adapters.state.write_update_state(finalized)
        logger.info("update: startup finalize -> job.state=%s job.target=%s", finalized.job.state, finalized.job.target)
    except Exception:
        logger.exception("update: finalize_after_restart failed at startup")

    try:
        ctx: AppsJobContext = app.state.apps_job_context
        for instance in ctx.sync_unit.list_active_instances():
            logger.warning("apps: stopped orphan sync unit instance=%s", instance)
            ctx.sync_unit.stop(instance)
        cleanup_staging_and_trash(ctx)
        apps_state = adapters.state.read_apps_state()
        # An unreadable or legacy ledger reads as empty; sweeping against it would trash every app.
        apps_file_state = adapters.state.apps_state_file_state()
        if apps_file_state is AppsStateFileState.PRESENT:
            sweep_orphan_app_dirs(ctx, apps_state)
        elif apps_file_state is AppsStateFileState.LEGACY:
            logger.error("apps: legacy ledger; skipping the orphan app directory sweep")
        else:
            logger.error("apps: ledger corrupt; skipping the orphan app directory sweep")
        manifest_exists, venv_exists = disk_state_checks(ctx, apps_state)
        finalized_apps = finalize_apps_state(
            apps_state, manifest_exists=manifest_exists, venv_exists=venv_exists, now=time.time()
        )
        if finalized_apps != apps_state:
            adapters.state.write_apps_state(finalized_apps)
        logger.info("apps: startup finalize -> current_job=%s", finalized_apps.current_job)
        reconcile_run_dirs(adapters.app_unit, adapters.run_dir, finalized_apps)
    except Exception:
        logger.exception("apps: finalize_apps_state failed at startup")

    try:
        platform_state = adapters.state.read_platform_update_state()
        finalized_platform = finalize_platform_after_restart(platform_state, time.time())
        if finalized_platform is not platform_state:
            adapters.state.write_platform_update_state(finalized_platform)
        logger.info(
            "app-platform: startup finalize -> state=%s step=%s",
            finalized_platform.job.state,
            finalized_platform.job.step,
        )
    except Exception:
        logger.exception("app-platform: finalize_after_restart failed at startup")

    try:
        logger.info("app-platform: startup verify begin")
        await anyio.to_thread.run_sync(_refresh_platform_readiness, app)
        logger.info("app-platform: startup verify end")
    except Exception:
        # install.sh verify is a sudo-backed subprocess call -- a hang or an unexpected
        # crash here must never keep the app from serving every other route.
        logger.exception("app-platform: startup verify failed")

    if app.state.settings.autostart_enabled:
        try:
            await anyio.to_thread.run_sync(_run_autostart, app)
        except Exception:
            logger.exception("autostart: startup autostart failed")

    periodic_thread: threading.Thread | None = None
    if app.state.settings.periodic_enabled:
        periodic_thread = threading.Thread(
            target=app.state.periodic_scheduler.run_forever, daemon=True, name="palmimo-portal-periodic"
        )
        periodic_thread.start()

    yield

    if periodic_thread is not None:
        app.state.periodic_scheduler.stop()
        periodic_thread.join(timeout=5.0)


def _refresh_platform_readiness(app: FastAPI) -> None:
    """Recompute and cache the platform-bundle verify result and log the ready/not-ready transition.

    Called at startup (before autostart, design doc 2.8) and after every
    successful platform-update job. ``GET /platform`` without
    ``?verify=true`` and ``StartDeps.platform_ready`` both read the cached
    result this writes rather than re-running ``install.sh verify`` on
    every request.
    """
    adapters: AdapterBundle = app.state.adapters
    settings: Settings = app.state.settings
    diffs = run_verify(adapters.platform, settings.platform_dir)
    app.state.platform_verify_diffs = diffs
    installed = adapters.platform.read_installed()
    status = compute_status(installed, settings.required_platform_version, diffs, None, None, None)
    if status["ready"]:
        logger.info("app-platform: ready platform_version=%s", status["installed_version"])
    else:
        missing = [(d["kind"], d["path"]) for d in status["verify_diffs"]]
        logger.warning(
            "app-platform: not ready reason=%s installed_version=%s diffs=%s journal_readable=%s",
            status["reason"],
            status["installed_version"],
            missing,
            adapters.journal.can_read(),
        )


def _run_autostart(app: FastAPI) -> None:
    """Start at most one `autostart: true` app at boot, name order (design doc 2.5).

    Runs after both finalize blocks above, so it only ever sees an
    already-reconciled ledger. Never raises out of startup: a device that
    boots with autostart merely skipped is better than one that fails to
    come up at all over one bad app.
    """
    adapters: AdapterBundle = app.state.adapters
    apps_state = adapters.state.read_apps_state()
    candidates = sorted(
        name for name, record in apps_state.apps.items() if record.autostart and record.broken_reason is None
    )
    if not candidates:
        logger.info("autostart: no app selected")
        return

    selected, *skipped = candidates
    for name in skipped:
        logger.warning("autostart: skipped name=%s", name)

    deps: StartDeps = app.state.start_deps
    if adapters.app_unit.status(selected).active_state in RUNNING_ACTIVE_STATES:
        # The unit survived a Portal restart on its own systemd lifecycle (e.g. a Portal
        # self-update) -- starting it again would reset DeviceAllow (always cleared first)
        # out from under the still-running process.
        logger.info("autostart: already running name=%s", selected)
        return

    logger.info("autostart: selected name=%s", selected)
    try:
        start_app(deps, selected, host=socket.gethostname())
    except AppNotFoundError as error:
        logger.warning("autostart: failed reason=%s", error)
    except (AppStartError, PolkitDeniedError) as error:
        reason = getattr(error, "code", None) or str(error)
        logger.warning("autostart: failed reason=%s", reason)
    except Exception:
        logger.exception("autostart: failed reason=unexpected_error")


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the Palmimo Portal FastAPI application.

    Adapter construction -- including, for ``settings.adapters == "real"``,
    the :func:`~palmimo_portal.adapters.state.preflight_state_dir` startup
    check -- is :func:`~palmimo_portal.wiring.build_adapters`'s job, not
    this function's: ``api/`` never imports ``adapters/`` directly (see
    ``tests/test_import_contracts.py``); only :mod:`palmimo_portal.wiring`
    and :mod:`palmimo_portal.testing` do.
    """
    if settings is None:
        settings = get_settings()

    adapters = build_adapters(settings)
    always_allowed_hosts = frozenset({"localhost", "127.0.0.1"}) | settings.allowed_hosts

    app = FastAPI(
        title="Palmimo Portal",
        docs_url="/docs" if settings.enable_docs else None,
        redoc_url="/redoc" if settings.enable_docs else None,
        openapi_url="/openapi.json" if settings.enable_docs else None,
        lifespan=_lifespan,
    )
    app.state.settings = settings
    app.state.adapters = adapters
    app.state.rate_limiter = LoginRateLimiter()
    app.state.reset_rate_limiter = ResetRateLimiter()
    app.state.update_lock = threading.Lock()
    # One UpdateRunner instance for the whole app's lifetime: its
    # `_busy_lock` is a real cross-request, cross-job guard, which only
    # works if every request shares the same instance.
    # `update_runner_alive` is the liveness flag that instance sets for
    # the duration of any job it runs -- see
    # `core.update.expire_stale_running` for why liveness, not a
    # wall-clock timeout, decides whether a "running" job is stuck.
    app.state.update_runner_alive = threading.Event()
    app.state.update_runner = UpdateRunner(
        adapters.state,
        adapters.updater,
        adapters.system,
        run_in_thread=bool(settings.update_run_in_thread),
        restart_delay_seconds=float(settings.update_restart_delay_seconds),
        alive=app.state.update_runner_alive,
    )
    # Built before AppsJobContext (below) so check_git_update's tag path can read it without
    # a network call of its own -- the periodic task and GET /catalog keep it warm.
    app.state.catalog_cache = CatalogCache(adapters.catalog, adapters.state)
    # One AppsJobContext/AppsJobRunner for the whole app's lifetime, for the same reason as
    # UpdateRunner above -- also reused by _lifespan's startup finalize.
    app.state.apps_job_context = AppsJobContext(
        apps_dir=settings.apps_dir,
        uv_cache_dir=settings.uv_cache_dir,
        git=adapters.git,
        uv=adapters.uv,
        sync_unit=adapters.sync_unit,
        journal=adapters.journal,
        disk=adapters.disk,
        secrets=adapters.secrets,
        app_unit=adapters.app_unit,
        state=adapters.state,
        run_dir=adapters.run_dir,
        catalog_cache=app.state.catalog_cache,
        catalog_repo=settings.catalog_repo,
    )
    app.state.apps_job_runner = AppsJobRunner(
        adapters.state, app.state.apps_job_context, adapters.app_unit, run_in_thread=bool(settings.apps_run_in_thread)
    )

    # Populated by _refresh_platform_readiness (startup, and after every successful
    # platform-update job) -- None until the first successful verification.
    app.state.platform_verify_diffs = None
    app.state.platform_latest_cache = PlatformLatestCache(
        adapters.platform_releases, adapters.platform_bundle, adapters.state, settings.platform_dir
    )
    app.state.platform_update_runner = PlatformUpdateRunner(
        adapters.state,
        adapters.platform_bundle,
        adapters.platform,
        adapters.system,
        settings.platform_dir,
        portal_installed_version=portal_version(),
        on_finish=lambda: _refresh_platform_readiness(app),
    )
    app.state.platform_job_runner = PlatformJobRunner(
        adapters.state, app.state.platform_update_runner, run_in_thread=bool(settings.apps_run_in_thread)
    )

    app.state.periodic_scheduler = build_scheduler(
        clock=adapters.clock,
        apps_job_context=app.state.apps_job_context,
        state_store=adapters.state,
        catalog_cache=app.state.catalog_cache,
        platform_latest_cache=app.state.platform_latest_cache,
    )

    def _platform_ready() -> bool:
        installed = adapters.platform.read_installed()
        status = compute_status(
            installed, settings.required_platform_version, app.state.platform_verify_diffs, None, None, None
        )
        return bool(status["ready"])

    app.state.start_deps = StartDeps(
        state=adapters.state,
        apps_job_ctx=app.state.apps_job_context,
        app_unit=adapters.app_unit,
        run_dir=adapters.run_dir,
        platform_ready=_platform_ready,
    )

    # Order matters: the last middleware added is the outermost, so adding
    # CSRF then Session then HostGuard makes requests hit HostGuard first.
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(SessionMiddleware)
    app.add_middleware(HostGuardMiddleware, always_allowed_hosts=always_allowed_hosts, network=adapters.network)

    # PortalError is an HTTPException subclass, so registering the base class
    # covers it too, alongside every other HTTPException FastAPI itself
    # raises (a 404 for an unmatched path, for instance).
    app.add_exception_handler(HTTPException, _handle_http_exception)
    app.add_exception_handler(RequestValidationError, _handle_validation_error)
    app.add_exception_handler(AdapterUnavailableError, _handle_adapter_unavailable)
    app.add_exception_handler(Exception, _handle_unexpected_error)

    app.include_router(system_api.router)
    app.include_router(auth_api.router)
    app.include_router(wifi_api.router)
    app.include_router(ssh_keys_api.router)
    app.include_router(update_api.router)
    app.include_router(apps_api.router)
    app.include_router(platform_api.router)
    app.include_router(secrets_api.router)
    app.include_router(catalog_api.router)

    _mount_frontend(app, settings.static_dir)

    _log_startup_banner(settings)

    return app


def _mount_frontend(app: FastAPI, static_dir: Path) -> None:
    """Serve the frontend's build output, with an SPA fallback to ``index.html``.

    Registered after every ``api/`` router, so an ``/api/...`` path always
    hits a router (404ing as JSON) before this catch-all ever runs. A no-op
    when ``static_dir`` does not exist -- a checkout without ``make build``
    has nothing to serve, and should 404 rather than raise at startup.

    Logged at WARNING, not INFO: the build ships as a GitHub Release asset
    the Updater fetches (doc/releasing.md), so an absent ``static/`` on a
    real deployment is a gap an operator needs to notice in ``journalctl``.
    """
    assets_dir = static_dir / "assets"
    if (
        not static_dir.is_dir()
        or not (static_dir / "index.html").is_file()
        or not assets_dir.is_dir()
        or not any(assets_dir.iterdir())
    ):
        # sync contract: an interrupted build or half-extracted release
        # asset can leave index.html present with assets/ missing (or vice
        # versa) -- StaticFiles would raise at mount time, crashing
        # create_app() instead of degrading to "API only". Treat any
        # incomplete combination as a missing build; `any(assets_dir.iterdir())`
        # also catches an empty assets/ dir, which StaticFiles mounts without raising.
        logger.warning(
            "frontend build not found (or incomplete) at %s -- run 'make build' in the repository root, or "
            "let the updater fetch the release asset; the API works, the UI will 404",
            static_dir,
        )
        return

    # Fixed-name build assets (see frontend/vite.config.ts) served directly.
    app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="frontend-assets")

    index_path = static_dir / "index.html"

    @app.get("/", include_in_schema=False)
    def _serve_index() -> FileResponse:
        return FileResponse(index_path)

    @app.get("/{full_path:path}", include_in_schema=False)
    def _serve_spa(full_path: str) -> FileResponse:
        """Serve a static file at ``full_path`` if one exists, else fall back to ``index.html``.

        The fallback makes client-side routes (``/login``, ``/setup``, ...)
        work on a hard refresh: there is no server-side route for them,
        only a TanStack Router route the bundle registers once it loads.
        """
        # `full_path == "api"` (no trailing slash) misses the "api/" prefix
        # check but is still an API path -- else GET /api 200s with index.html.
        if full_path.startswith(API_PATH_PREFIX) or full_path == API_PATH_PREFIX.rstrip("/"):
            raise HTTPException(status_code=404, detail={"code": "not_found", "params": {}})
        candidate = static_dir / full_path
        if full_path and candidate.is_file():
            # is_file() above follows `..`; resolve().relative_to() rejects
            # a result outside static_dir before FileResponse opens it.
            try:
                candidate.resolve().relative_to(static_dir.resolve())
            except ValueError as error:
                raise HTTPException(status_code=404, detail={"code": "not_found", "params": {}}) from error
            return FileResponse(candidate)
        return FileResponse(index_path)


def _log_startup_banner(settings: Settings) -> None:
    """Log a one-line startup summary, at WARNING when running on fake adapters.

    Fake adapters are correct for local dev/CI but silently wrong on a
    shipped device -- WARNING makes that visible in ``journalctl`` unprompted.
    """
    banner = (
        f"palmimo-portal {portal_version()} starting: "
        f"adapters={settings.adapters} port={settings.port} state_dir={settings.state_dir} "
        f"allowed_hosts={sorted(settings.allowed_hosts)}"
    )
    if settings.adapters == "fake":
        logger.warning(banner)
    else:
        logger.info(banner)
