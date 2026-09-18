"""Readiness status and the update job driver for the realtime platform bundle (design doc 2.8).

Two independent pieces:

- :func:`compute_status` -- pure. ``ready`` requires an installed bundle at
  or above :attr:`~palmimo_portal.settings.Settings.required_platform_version`
  *and* an empty ``verify_diffs``. Consumed by ``GET /platform`` and by
  :attr:`~palmimo_portal.core.apps_start.StartDeps.platform_ready`.
- :class:`PlatformUpdateRunner` -- drives one job through the design doc
  2.8 pipeline: ``fetch -> verify_sha -> extract`` (:class:`~palmimo_portal.ports.PlatformBundleSource`),
  ``preflight`` (this module, reading the freshly-extracted ``manifest.json``),
  ``install -> verify -> record`` (:class:`~palmimo_portal.ports.PlatformPort`),
  then a best-effort ``restart`` if the manifest asks for one.

Unlike :mod:`palmimo_portal.core.update_runner`, a platform job's success is
decided entirely by its own ``record`` step, *before* ``restart_portal`` is
ever called -- so there is no "restarting" limbo state to resolve after a
restart the way a Portal self-update needs (:mod:`palmimo_portal.core.platform_update`).
"""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from palmimo_portal.core.platform_update import advance, mark_done, mark_failed, start_update
from palmimo_portal.core.update import CHECK_RATE_LIMIT_SECONDS
from palmimo_portal.ports import (
    AdapterUnavailableError,
    PlatformBundleFetchError,
    PlatformBundleSource,
    PlatformCommandError,
    PlatformInstalled,
    PlatformLockTimeoutError,
    PlatformManifest,
    PlatformPort,
    PlatformVerifyDiff,
    Release,
    ReleaseSource,
    ReleaseSourceError,
    StateStore,
    SystemPort,
)


logger = logging.getLogger("palmimo_portal")

#: How long a fetched "latest release" answer is reused before asking GitHub
#: again -- mirrors the catalog's 1-hour cache (design doc 4.1) closely
#: enough; the doc leaves this exact figure unspecified for the platform
#: channel, so the same interval class (device-update-frequency, not
#: request-frequency) is used here too.
LATEST_CACHE_SECONDS = 3600.0

#: The bundle last fetched (by an update job, or by a status/verify check)
#: is kept here so verify can run again without re-downloading.
CURRENT_BUNDLE_DIRNAME = "current"
STAGING_DIRNAME = "staging"


def parse_manifest(bundle_dir: Path) -> PlatformManifest:
    """Read and validate ``<bundle_dir>/manifest.json``.

    Raises:
        ValueError: the file is missing, is not valid JSON, or is missing a required key.
    """
    path = bundle_dir / "manifest.json"
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ValueError(f"cannot read {path}: {error}") from error
    try:
        return PlatformManifest(
            version=int(data["version"]),
            requires_portal=str(data["requires_portal"]),
            restart_portal=bool(data["restart_portal"]),
            summary=str(data["summary"]),
            reflash_required=bool(data.get("reflash_required", False)),
        )
    except (KeyError, TypeError, ValueError) as error:
        raise ValueError(f"{path} is missing or has a malformed field: {error}") from error


def _version_tuple(text: str) -> tuple[int, ...]:
    """Parse a dotted version string (an optional leading ``v`` stripped) into a comparable tuple.

    Falls back to ``(0,)`` for anything unparseable -- a malformed
    ``requires_portal``/installed-version string must never crash a
    readiness check; it is treated as "not newer", the safe direction for
    :func:`portal_satisfies_requirement`.
    """
    text = text.removeprefix("v")
    parts: list[int] = []
    for chunk in text.split("."):
        digits = "".join(ch for ch in chunk if ch.isdigit())
        if not digits:
            break
        parts.append(int(digits))
    return tuple(parts) or (0,)


def portal_satisfies_requirement(installed_portal_version: str, requires_portal: str) -> bool:
    """Report whether ``installed_portal_version`` is at least ``requires_portal``."""
    return _version_tuple(installed_portal_version) >= _version_tuple(requires_portal)


def compute_status(
    installed: PlatformInstalled | None,
    required_version: int,
    verify_diffs: list[PlatformVerifyDiff] | None,
    latest: PlatformManifest | None,
    latest_tag: str | None,
    latest_error: str | None,
) -> dict[str, Any]:
    """Combine the installed bundle, its verify diffs, and the discovered latest release into one status dict.

    ``verify_diffs`` of ``None`` means verify could not run at all (no
    cached bundle to run ``install.sh verify`` against yet, or the command
    itself failed/timed out) -- distinct from ``[]`` (ran and found nothing
    wrong). Not ready in either case: a bundle that has never actually been
    verified must not be reported as clean.
    """
    reason: str | None
    if installed is None:
        ready = False
        reason = "platform_not_installed"
    elif installed.version < required_version:
        ready = False
        reason = "platform_outdated"
    elif verify_diffs is None:
        ready = False
        reason = "platform_verify_unavailable"
    elif verify_diffs:
        ready = False
        reason = "platform_verify_failed"
    else:
        ready = True
        reason = None
    return {
        "installed_version": installed.version if installed is not None else None,
        "installed_at": installed.installed_at if installed is not None else None,
        "required_version": required_version,
        "ready": ready,
        "reason": reason,
        "verify_diffs": [
            {"path": d.path, "kind": d.kind, "expected": d.expected, "actual": d.actual} for d in (verify_diffs or [])
        ],
        "latest": (
            {
                "version": latest.version,
                "tag": latest_tag,
                "summary": latest.summary,
                "requires_portal": latest.requires_portal,
                "restart_portal": latest.restart_portal,
                "reflash_required": latest.reflash_required,
            }
            if latest is not None
            else None
        ),
        "latest_error": latest_error,
    }


def current_bundle_dir(platform_dir: Path) -> Path:
    """Where the last successfully fetched/installed bundle is cached for repeat ``verify`` calls."""
    return platform_dir / CURRENT_BUNDLE_DIRNAME


def run_verify(platform_port: PlatformPort, platform_dir: Path) -> list[PlatformVerifyDiff] | None:
    """Run ``install.sh verify`` against the cached bundle, or return ``None`` if none is cached.

    Never raises: a :class:`~palmimo_portal.ports.PlatformCommandError`
    (a crashed/timed-out ``install.sh``) is logged and reported as
    "could not verify" rather than crashing the startup/periodic caller.
    """
    bundle_dir = current_bundle_dir(platform_dir)
    if not (bundle_dir / "install.sh").is_file():
        return None
    try:
        return platform_port.verify(bundle_dir)
    except PlatformCommandError as error:
        logger.error("app-platform: verify failed: %s", error)
        return None


#: How long a failed latest-release attempt (network error, rate limit, a manifest that fails
#: to parse) is left alone before the next `get()` retries -- much shorter than
#: `LATEST_CACHE_SECONDS` so a transient GitHub outage does not hide a real update for an hour,
#: but still short enough to stop every request from re-hitting GitHub during an outage.
LATEST_BACKOFF_SECONDS = 300.0


class PlatformCheckRateLimitedError(Exception):
    """Raised by :meth:`PlatformLatestCache.check_now` when the last successful forced check
    was under :data:`~palmimo_portal.core.update.CHECK_RATE_LIMIT_SECONDS` ago -- the same
    cadence ``POST /api/v1/update/check`` enforces for a Portal self-update check.
    """

    def __init__(self, retry_after_seconds: float) -> None:
        self.retry_after_seconds = retry_after_seconds
        super().__init__(f"retry after {retry_after_seconds:.0f}s")


class PlatformLatestCache:
    """In-process, TTL'd cache of the latest platform release's manifest (design doc 2.8).

    One instance lives on ``app.state`` for the process's lifetime. Not
    thread-safe against overlapping fetches (a lost update at worst
    duplicates one GitHub round trip); the actual job driver
    (:class:`PlatformUpdateRunner`) does not use this cache.
    """

    def __init__(
        self,
        release_source: ReleaseSource,
        bundle_source: PlatformBundleSource,
        state_store: StateStore,
        platform_dir: Path,
        *,
        ttl_seconds: float = LATEST_CACHE_SECONDS,
        backoff_seconds: float = LATEST_BACKOFF_SECONDS,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._release_source = release_source
        self._bundle_source = bundle_source
        self._state_store = state_store
        self._platform_dir = platform_dir
        self._ttl_seconds = ttl_seconds
        self._backoff_seconds = backoff_seconds
        self._now = now
        self._fetched_at: float | None = None
        self._failed_at: float | None = None
        self._manifest: PlatformManifest | None = None
        self._tag: str | None = None
        self._error: str | None = None

    def get(
        self, *, ntp_synchronized: bool, force: bool = False
    ) -> tuple[PlatformManifest | None, str | None, str | None]:
        """Return ``(manifest, tag, error)``, refreshing from GitHub if stale, backed off, or ``force``."""
        if not force and self._recently_attempted():
            return self._manifest, self._tag, self._error
        self._refresh(ntp_synchronized=ntp_synchronized)
        return self._manifest, self._tag, self._error

    def check_now(self, *, ntp_synchronized: bool) -> tuple[PlatformManifest | None, str | None, str | None]:
        """Bypass the TTL and fetch the latest release now, for a user-initiated "check now".

        Rate-limited against ``_fetched_at`` the same way
        :func:`~palmimo_portal.core.update.start_check` rate-limits against
        ``UpdateState.checked_at``: only a *successful* fetch resets the
        window, so a failed attempt (network error, rate limit) never
        blocks an immediate retry.

        Raises:
            PlatformCheckRateLimitedError: the last successful fetch was under
                :data:`~palmimo_portal.core.update.CHECK_RATE_LIMIT_SECONDS` ago.
        """
        if self._fetched_at is not None:
            elapsed = self._now() - self._fetched_at
            if 0 <= elapsed < CHECK_RATE_LIMIT_SECONDS:
                raise PlatformCheckRateLimitedError(CHECK_RATE_LIMIT_SECONDS - elapsed)
        return self.get(ntp_synchronized=ntp_synchronized, force=True)

    def _recently_attempted(self) -> bool:
        if self._fetched_at is not None and self._now() - self._fetched_at < self._ttl_seconds:
            return True
        return self._failed_at is not None and self._now() - self._failed_at < self._backoff_seconds

    def _refresh(self, *, ntp_synchronized: bool) -> None:
        if not ntp_synchronized:
            # No `_failed_at` set here -- a clock that syncs mid-TTL must be retried on the very
            # next call, not wait out a backoff window that was never a real failed attempt.
            self._error = "clock_unsynced"
            return
        try:
            with self._state_store.lock_platform():
                pass
        except PlatformLockTimeoutError:
            # `platform.lock` is held by a running update job -- its staging directory is
            # exclusive to that job (design doc 2.8). Skip this refresh entirely rather than
            # touch `platform_dir`; the previous cached answer (possibly still `None`) stands.
            return
        try:
            release: Release = self._release_source.fetch_latest()
            manifest = self._fetch_manifest(release.tag)
        except (ReleaseSourceError, PlatformBundleFetchError, ValueError) as error:
            code = getattr(error, "code", None)
            self._manifest, self._tag = None, None
            self._error = "rate_limited" if code == "rate_limited" else str(error)
            self._failed_at = self._now()
            return
        self._manifest, self._tag, self._error = manifest, release.tag, None
        self._fetched_at, self._failed_at = self._now(), None

    def _fetch_manifest(self, tag: str) -> PlatformManifest:
        """Prefer the small ``.manifest.json`` release asset; fall back to extracting the tarball.

        The fallback's staging directory is this call's own
        ``tempfile.mkdtemp`` under ``platform_dir`` -- never
        ``PlatformUpdateRunner``'s ``STAGING_DIRNAME``, which an update job
        may be using concurrently for something this method must not
        observe or disturb.
        """
        try:
            return self._bundle_source.fetch_latest_manifest(tag)
        except PlatformBundleFetchError:
            pass
        self._platform_dir.mkdir(parents=True, exist_ok=True)
        staging_root = Path(tempfile.mkdtemp(dir=self._platform_dir))
        try:
            bundle_dir, _sha = self._bundle_source.fetch(tag, staging_root, on_step=lambda _step: None)
            return parse_manifest(bundle_dir)
        finally:
            shutil.rmtree(staging_root, ignore_errors=True)


class PlatformUpdateRunner:
    """Runs one platform-bundle update job (design doc 2.8's pipeline) to completion.

    ``api/platform.py`` owns the one-job-at-a-time guarantee
    (:meth:`~palmimo_portal.ports.StateStore.lock_platform`) and the
    mutual-exclusion probes against a Portal self-update / app job, the
    same way ``api/update.py`` and ``api/apps.py`` do for their own axes.
    """

    def __init__(
        self,
        state_store: StateStore,
        bundle_source: PlatformBundleSource,
        platform_port: PlatformPort,
        system: SystemPort,
        platform_dir: Path,
        *,
        portal_installed_version: str,
        on_finish: Callable[[], None] | None = None,
    ) -> None:
        self._state = state_store
        self._bundle_source = bundle_source
        self._platform_port = platform_port
        self._system = system
        self._platform_dir = platform_dir
        self._portal_installed_version = portal_installed_version
        #: Called once, unconditionally, at the end of `run` -- lets a caller (api/app.py)
        #: refresh its cached readiness snapshot without this module knowing about FastAPI.
        self._on_finish = on_finish

    def run(self, tag: str, target_version: int) -> None:
        """Run the whole pipeline for *tag*/*target_version* synchronously, in the caller's thread.

        ``api/platform.py`` is responsible for backgrounding this call on a
        daemon thread, mirroring :class:`~palmimo_portal.core.update_runner.UpdateRunner`
        (a plain method, not self-threading, so tests can call it inline).
        Calls ``on_finish`` (if given) exactly once, whether the job
        succeeded or failed -- and, if the manifest asked for a restart,
        strictly *after* that call returns: ``on_finish`` refreshes the
        cached readiness snapshot this (still-running) process serves,
        which must happen before ``restart_portal`` can end the process.
        """
        should_restart = False
        try:
            should_restart = self._run(tag, target_version)
        finally:
            if self._on_finish is not None:
                self._on_finish()
        if should_restart:
            try:
                self._system.restart_portal()
            except AdapterUnavailableError as error:
                logger.warning("app-platform: restart_portal failed after applying %s: %s", tag, error)

    def _run(self, tag: str, target_version: int) -> bool:
        """Run the pipeline, returning whether the manifest asked for a restart on success.

        Every step already catches its own expected failure mode
        (:class:`~palmimo_portal.ports.PlatformBundleFetchError`,
        :class:`~palmimo_portal.ports.PlatformCommandError`, a malformed
        manifest) and returns ``False``. The outer ``except Exception``
        is the backstop for anything else -- a bug, or an
        :class:`OSError` from ``shutil``/:class:`StateStore` writes (e.g.
        a full disk) -- so a crash mid-job still leaves a terminal
        ``failed`` state and releases ``platform.lock``
        (:class:`PlatformJobRunner`'s ``finally``) instead of leaving the
        job stuck ``running`` forever.
        """
        current_step = "fetch"

        def on_step(step: str) -> None:
            nonlocal current_step
            current_step = step
            logger.info("app-platform: update job step=%s target=%s", step, target_version)
            self._state.write_platform_update_state(advance(self._state.read_platform_update_state(), step))

        def fail(step: str, message: str) -> None:
            logger.warning("app-platform: update job failed step=%s target=%s: %s", step, target_version, message)
            self._state.write_platform_update_state(
                mark_failed(self._state.read_platform_update_state(), step, message, time.time())
            )

        try:
            return self._run_pipeline(tag, on_step, fail)
        except Exception as error:
            logger.exception("app-platform: update job crashed step=%s target=%s", current_step, target_version)
            try:
                fail(current_step, f"unexpected: {type(error).__name__}")
            except Exception:
                logger.exception("app-platform: could not record the crash above -- job state may be stale")
            return False

    def _run_pipeline(self, tag: str, on_step: Callable[[str], None], fail: Callable[[str, str], None]) -> bool:
        staging_parent = self._platform_dir / STAGING_DIRNAME
        try:
            bundle_dir, sha = self._bundle_source.fetch(tag, staging_parent, on_step)
        except PlatformBundleFetchError as error:
            fail(error.step, str(error))
            return False

        on_step("preflight")
        try:
            manifest = parse_manifest(bundle_dir)
        except ValueError as error:
            fail("preflight", str(error))
            shutil.rmtree(staging_parent, ignore_errors=True)
            return False
        if manifest.reflash_required:
            fail("preflight", "reflash_required")
            shutil.rmtree(staging_parent, ignore_errors=True)
            return False
        if not portal_satisfies_requirement(self._portal_installed_version, manifest.requires_portal):
            fail("preflight", "portal_too_old")
            shutil.rmtree(staging_parent, ignore_errors=True)
            return False

        on_step("install")
        try:
            self._platform_port.install(bundle_dir)
        except PlatformCommandError as error:
            fail("install", str(error))
            shutil.rmtree(staging_parent, ignore_errors=True)
            return False

        on_step("verify")
        try:
            diffs = self._platform_port.verify(bundle_dir)
        except PlatformCommandError as error:
            fail("verify", str(error))
            shutil.rmtree(staging_parent, ignore_errors=True)
            return False
        if diffs:
            fail("verify", f"{len(diffs)} difference(s) after install")
            shutil.rmtree(staging_parent, ignore_errors=True)
            return False

        on_step("record")
        try:
            self._platform_port.record(bundle_dir, sha)
        except PlatformCommandError as error:
            fail("record", str(error))
            shutil.rmtree(staging_parent, ignore_errors=True)
            return False

        self._state.write_platform_update_state(mark_done(self._state.read_platform_update_state(), time.time()))
        shutil.rmtree(staging_parent, ignore_errors=True)

        if manifest.restart_portal:
            on_step("restart")
            return True
        return False


class PlatformJobRunner:
    """Serializes and backgrounds :meth:`PlatformUpdateRunner.run` calls.

    Lock handoff across threads, same shape as
    :class:`~palmimo_portal.core.apps_job_runner.AppsJobRunner`:
    :meth:`~palmimo_portal.ports.StateStore.lock_platform` is entered in the
    caller's thread (so contention raises
    :class:`~palmimo_portal.ports.PlatformLockTimeoutError` immediately,
    before any thread is spawned) and released in the worker thread once
    the pipeline finishes.
    """

    def __init__(self, state_store: StateStore, updater: PlatformUpdateRunner, *, run_in_thread: bool = True) -> None:
        self._state = state_store
        self._updater = updater
        self._run_in_thread = run_in_thread

    def start(self, tag: str, target_version: int) -> None:
        """Transition the job to ``"running"`` and start the pipeline, or raise if one is already in flight.

        Raises:
            PlatformLockTimeoutError: another platform update already holds ``platform.lock``.
            PlatformUpdateInProgressError: the persisted job is already ``"running"``
                (defense in depth -- the lock above is the primary guard).
        """
        lock_cm = self._state.lock_platform()
        lock_cm.__enter__()
        try:
            state = self._state.read_platform_update_state()
            new_state = start_update(state, target_version, time.time())
            self._state.write_platform_update_state(new_state)
        except BaseException:
            lock_cm.__exit__(None, None, None)
            raise

        def run() -> None:
            try:
                self._updater.run(tag, target_version)
            finally:
                lock_cm.__exit__(None, None, None)

        if self._run_in_thread:
            threading.Thread(target=run, daemon=True, name="palmimo-portal-platform-update").start()
        else:
            run()
