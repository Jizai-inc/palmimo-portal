"""Real :class:`~palmimo_portal.ports.StateStore`: JSON files under the state directory.

Schema, from the technical design's state-directory layout::

    <state_dir>/
      auth.json               -> password hash + session signing key (0600)
      last_attempt.json       -> most recent Wi-Fi connect attempt
      initial_session_key.json -> signing key for initial-mode sessions,
                                   created lazily on first login while
                                   auth.json is absent and an identity file
                                   is present (0600)

Directory creation is left to the caller (systemd's ``StateDirectory=`` on
the real device; a test's ``tmp_path`` fixture here) — this adapter only
writes files inside a directory it is given, other than the explicit
:func:`preflight_state_dir` startup check.

``last_attempt.json`` is not security-bearing: every read tolerates a
missing, corrupt, or wrong-shaped file by treating it as absent, and a
write simply replaces it. Reads that hit a decode or shape error log it at
ERROR, naming the path.

``auth.json`` is different, because "no password set" and "the file exists
but cannot be read" must never be confused: the former is the legitimate
out-of-box state where ``/setup`` is meant to run, and the latter is a
device that already has an owner whose file happens to be unreadable (a
crash mid-write, disk corruption, tampering). Collapsing that distinction
would let anyone on the LAN claim an already-owned device after a crash
corrupts its auth file. :meth:`JsonFileStateStore.read_auth` still returns
``None`` for both cases, but :meth:`JsonFileStateStore.auth_state` reports
:class:`~palmimo_portal.ports.AuthFileState.CORRUPT` distinctly, and
``api/auth.py`` checks it before touching ``read_auth``/``create_auth`` — a
corrupt file makes both ``/setup`` and ``/login`` answer 409
``auth_state_corrupt`` instead of running as if unprovisioned.

**Recovery is manual and out-of-band**: the only way out of ``CORRUPT`` is
an operator deleting ``auth.json`` over SSH (using a registered key --
``ssh-keys`` endpoints do not require Wi-Fi provisioning to have ever
succeeded), returning the file to ``ABSENT`` and letting ``/setup`` run
again. This module never auto-deletes a corrupt file itself: doing so from
inside a read path would reintroduce the same fail-open bug, just with one
extra step.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import secrets
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from palmimo_portal.adapters.atomic_write import atomic_write_text, create_exclusive_text, ensure_private_dir, fsync_dir
from palmimo_portal.core.auth import AUTH_LOCK_TIMEOUT_SECONDS
from palmimo_portal.core.update import IDLE_UPDATE_JOB, IDLE_UPDATE_STATE, is_valid_release_tag
from palmimo_portal.ports import (
    AuthAlreadyExistsError,
    AuthFileState,
    AuthLockTimeoutError,
    AuthState,
    Release,
    StateStore,
    UpdateJob,
    UpdateState,
    WifiAttempt,
)


logger = logging.getLogger("palmimo_portal")

AUTH_FILENAME = "auth.json"
LAST_ATTEMPT_FILENAME = "last_attempt.json"
INITIAL_SESSION_KEY_FILENAME = "initial_session_key.json"
AUTH_LOCK_FILENAME = ".auth.json.lock"
UPDATE_STATE_FILENAME = "update.json"

#: How often :meth:`JsonFileStateStore.lock_auth` retries a non-blocking
#: ``flock`` while waiting -- short enough to honor
#: :data:`~palmimo_portal.core.auth.AUTH_LOCK_TIMEOUT_SECONDS` to within a
#: small margin, not so short it busy-spins.
_AUTH_LOCK_POLL_INTERVAL_SECONDS = 0.05


#: The full :class:`~palmimo_portal.ports.UpdateJobState` /
#: :class:`~palmimo_portal.ports.UpdateJobKind` literal sets, mirrored here
#: so :meth:`JsonFileStateStore._parse_update_state` can validate with a
#: plain ``in`` check.
_VALID_UPDATE_JOB_STATES = frozenset({"idle", "checking", "running", "restarting", "done", "failed"})
_VALID_UPDATE_JOB_KINDS = frozenset({"update", "rollback"})


def _require_optional_type(value: Any, expected: type | tuple[type, ...], field_name: str) -> None:
    """Raise :class:`TypeError` unless ``value`` is ``None`` or an instance of ``expected``.

    ``bool`` is excluded even where ``int`` is one of ``expected`` --
    ``isinstance(True, int)`` is ``True`` in Python, which would let a stray
    JSON ``true``/``false`` through as a fake timestamp.
    """
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, expected):
        raise TypeError(f"{field_name} has an unexpected type: {value!r}")


_ORPHAN_TEMP_GLOB = ".*.tmp"

#: A temp file must be at least this old before the startup sweep considers
#: it orphaned. Applied defensively: an overlapping restart (the old
#: process's write-temp-then-rename still mid-flight while a new one starts
#: up) must not let the new sweep unlink a temp file the old process hasn't
#: renamed away yet, silently losing that write.
_ORPHAN_TEMP_MIN_AGE_SECONDS = 60.0


def _sweep_orphan_temp_files(state_dir: Path, *, min_age_seconds: float = _ORPHAN_TEMP_MIN_AGE_SECONDS) -> None:
    """Remove leftover ``atomic_write_text``/``create_exclusive_text`` temp files older than *min_age_seconds*, logging each one.

    Both write helpers create their temp file as ``.<name>.<random>.tmp`` in
    this same directory, then rename it onto the final path. A crash
    between those two steps leaves the temp file behind forever, so without
    this sweep it would accumulate, invisible, across restarts. Only run at
    startup, and only ever removes files matching the exact orphan shape --
    a real state file does not start with ``.`` or end with ``.tmp``.

    **Single-instance assumption**: this adapter assumes exactly one process
    holds ``state_dir`` at a time (one systemd unit, one
    ``StateDirectory=``). The age threshold is this function's only
    protection against a *second*, unexpected process racing an in-flight
    write -- not a substitute for actually supporting two instances.
    """
    now = time.time()
    for orphan in sorted(state_dir.glob(_ORPHAN_TEMP_GLOB)):
        try:
            age = now - orphan.stat().st_mtime
        except OSError as error:
            logger.error("failed to stat orphaned temp file %s: %s", orphan, error)
            continue
        if age < min_age_seconds:
            continue
        try:
            orphan.unlink()
        except OSError as error:
            logger.error("failed to remove orphaned temp file %s: %s", orphan, error)
            continue
        logger.info("removed orphaned temp file left over from an interrupted write: %s", orphan)


def preflight_state_dir(state_dir: Path) -> None:
    """Create ``state_dir`` (mode ``0700``), probe-write it, and sweep orphan temp files -- before anything else starts.

    Called at startup, only when ``PALMIMO_ADAPTERS=real``, so an
    unwritable or root-owned state directory surfaces as a clear error
    before uvicorn binds, rather than an opaque request-time 500. The
    orphan-temp-file sweep (:func:`_sweep_orphan_temp_files`) runs here too
    -- startup is the one moment this process can safely assume no write in
    this directory is in flight.

    Raises:
        RuntimeError: the directory could not be created or is not
            writable. The original :class:`OSError` (with its ``errno``)
            is chained as the cause.
    """
    probe_path = state_dir / f".preflight-{os.getpid()}"
    try:
        ensure_private_dir(state_dir)
        probe_path.write_text("", encoding="utf-8")
        probe_path.unlink()
    except OSError as error:
        raise RuntimeError(
            f"palmimo-portal cannot use state_dir={state_dir}: {error.strerror} (errno {error.errno})"
        ) from error
    _sweep_orphan_temp_files(state_dir)


class JsonFileStateStore(StateStore):
    """Persists :class:`AuthState` and :class:`WifiAttempt` as JSON files.

    Every write goes through :func:`~palmimo_portal.adapters.atomic_write.atomic_write_text`:
    a temp file in the state directory, permissioned ``0600`` from creation,
    then renamed onto the target -- a crash mid-write never leaves a
    truncated file, and the file is never briefly group/other-readable.
    """

    def __init__(self, state_dir: Path) -> None:
        self._state_dir = state_dir

    @property
    def _auth_path(self) -> Path:
        return self._state_dir / AUTH_FILENAME

    @property
    def _last_attempt_path(self) -> Path:
        return self._state_dir / LAST_ATTEMPT_FILENAME

    @property
    def _initial_session_key_path(self) -> Path:
        return self._state_dir / INITIAL_SESSION_KEY_FILENAME

    @property
    def _auth_lock_path(self) -> Path:
        return self._state_dir / AUTH_LOCK_FILENAME

    @property
    def _update_state_path(self) -> Path:
        return self._state_dir / UPDATE_STATE_FILENAME

    @contextlib.contextmanager
    def lock_auth(self) -> Iterator[None]:
        """Hold an exclusive ``flock`` on a dedicated lockfile for the block's duration, bounded.

        Same lockfile-next-to-``auth.json`` pattern as
        :meth:`~palmimo_portal.adapters.ssh_keys.AuthorizedKeysSshKeyPort._locked`.
        Retries a non-blocking ``flock`` every
        :data:`_AUTH_LOCK_POLL_INTERVAL_SECONDS` up to
        :data:`~palmimo_portal.core.auth.AUTH_LOCK_TIMEOUT_SECONDS`, logging
        a WARNING the moment it starts waiting.

        Raises:
            AuthLockTimeoutError: the lock could not be acquired within
                :data:`~palmimo_portal.core.auth.AUTH_LOCK_TIMEOUT_SECONDS`.
        """
        ensure_private_dir(self._state_dir)
        fd = os.open(self._auth_lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + AUTH_LOCK_TIMEOUT_SECONDS
            waited = False
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waited:
                        logger.warning(
                            "auth lock contended, waiting up to %gs: %s",
                            AUTH_LOCK_TIMEOUT_SECONDS,
                            self._auth_lock_path,
                        )
                        waited = True
                    if time.monotonic() >= deadline:
                        raise AuthLockTimeoutError() from None
                    time.sleep(_AUTH_LOCK_POLL_INTERVAL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    @staticmethod
    def _parse_auth(text: str) -> AuthState:
        """Parse ``auth.json`` text, raising on any decode, shape, or type problem.

        Valid JSON of the wrong shape must be rejected just as loudly as
        invalid JSON -- both are "corrupt" from the caller's point of view.
        """
        data: Any = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError(f"auth.json top-level value is a {type(data).__name__}, expected an object")
        password_hash, signing_key = data["password_hash"], data["signing_key"]
        if not isinstance(password_hash, str) or not isinstance(signing_key, str):
            raise TypeError("auth.json password_hash/signing_key must be strings")
        return AuthState(password_hash=password_hash, signing_key=signing_key)

    def read_auth(self) -> AuthState | None:
        if not self._auth_path.is_file():
            return None
        try:
            return self._parse_auth(self._auth_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as error:
            logger.error("state file is corrupt or unreadable, treating as absent: %s (%s)", self._auth_path, error)
            return None

    def auth_state(self) -> AuthFileState:
        if not self._auth_path.is_file():
            return AuthFileState.ABSENT
        try:
            self._parse_auth(self._auth_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as error:
            logger.error("auth state file is corrupt or unreadable: %s (%s)", self._auth_path, error)
            return AuthFileState.CORRUPT
        return AuthFileState.PRESENT

    def create_auth(self, state: AuthState) -> None:
        payload = {"password_hash": state.password_hash, "signing_key": state.signing_key}
        try:
            create_exclusive_text(self._auth_path, json.dumps(payload))
        except FileExistsError as error:
            raise AuthAlreadyExistsError() from error
        except OSError as error:
            logger.error("failed to create state file %s: %s", self._auth_path, error)
            raise

    def write_auth(self, state: AuthState) -> None:
        payload = {"password_hash": state.password_hash, "signing_key": state.signing_key}
        try:
            atomic_write_text(self._auth_path, json.dumps(payload))
        except OSError as error:
            logger.error("failed to write state file %s: %s", self._auth_path, error)
            raise

    def delete_auth(self) -> None:
        """Remove ``auth.json`` under :meth:`lock_auth`, and rotate an existing initial-mode key.

        See :meth:`~palmimo_portal.ports.StateStore.delete_auth`'s docstring
        for why the initial-mode key is rotated in place, and only when a
        key file already exists (not eagerly created here, preserving
        :meth:`read_or_create_initial_signing_key`'s lazy-creation
        contract).

        The key rotation happens **before** the ``auth.json`` unlink,
        deliberately: if this method raises partway through, the caller
        (``api/auth.py``'s ``reset``) must be left at worst with "nothing
        changed yet", never "credentials gone, but a stale initial-mode
        cookie still verifies" -- unlinking first would leave exactly that
        window if the rotation write then failed.
        """
        with self.lock_auth():
            if self._initial_session_key_path.is_file():
                fresh_key = secrets.token_urlsafe(32)
                atomic_write_text(self._initial_session_key_path, json.dumps({"signing_key": fresh_key}))
            existed = self._auth_path.exists()
            self._auth_path.unlink(missing_ok=True)
            if existed:
                fsync_dir(self._state_dir)

    @staticmethod
    def _parse_wifi_attempt(text: str) -> WifiAttempt:
        """Parse ``last_attempt.json`` text, raising on any decode, shape, or type problem.

        Not security-bearing, but still validated the same way ``auth.json``
        is: a wrong-shaped payload must be treated as absent, not raise past
        this adapter's tolerant-read contract into a 500.
        """
        data: Any = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError(f"last_attempt.json top-level value is a {type(data).__name__}, expected an object")
        ssid, result, timestamp = data["ssid"], data["result"], data["timestamp"]
        if not isinstance(ssid, str) or not isinstance(result, str) or not isinstance(timestamp, int | float):
            raise TypeError("last_attempt.json ssid/result/timestamp have unexpected types")
        observed_connection_name = data.get("observed_connection_name")
        if observed_connection_name is not None and not isinstance(observed_connection_name, str):
            raise TypeError("last_attempt.json observed_connection_name must be a string or absent/null")
        # `api/wifi.py`'s connect handler validates a fresh ssid before ever
        # reaching this file, but an already-poisoned record written before
        # that validation existed (or the equally-untrusted
        # observed_connection_name, sourced from the adapter's own read of
        # the system) can still contain a lone surrogate -- valid JSON to
        # the stdlib decoder, not valid UTF-8. Left unchecked, that value
        # would make every later serialization of this attempt (e.g.
        # `GET /system/status`) raise. Encode-checking here, rather than
        # trusting the writer, lets :meth:`read_last_wifi_attempt` catch
        # this case and delete the poisoned file outright -- the read heals
        # by removing the file, not merely by masking it on every call.
        for value in (ssid, observed_connection_name):
            if value is not None:
                value.encode("utf-8")
        return WifiAttempt(
            ssid=ssid, result=result, timestamp=float(timestamp), observed_connection_name=observed_connection_name
        )

    def read_last_wifi_attempt(self) -> WifiAttempt | None:
        if not self._last_attempt_path.is_file():
            return None
        try:
            return self._parse_wifi_attempt(self._last_attempt_path.read_text(encoding="utf-8"))
        except UnicodeEncodeError as error:
            # Masking without deleting would re-warn on every ~10s status
            # poll forever -- thousands of journal lines a day for a device
            # that never recovers on its own. Delete outright instead, so
            # this is the last time this particular poisoned file is ever
            # seen. No lock: `write_last_wifi_attempt` writes this file
            # lock-free too (atomic_write_text's temp-then-rename is the
            # only atomicity this file gets), so `missing_ok=True` alone is
            # enough to make a concurrent deleter (another read racing the
            # same file, or an operator over SSH) a silent no-op here
            # rather than a crash.
            self._last_attempt_path.unlink(missing_ok=True)
            logger.warning(
                "state file contained an ssid/observed name that cannot encode to UTF-8, deleted it: %s (%s)",
                self._last_attempt_path,
                error,
            )
            return None
        except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as error:
            logger.error(
                "state file is corrupt or unreadable, treating as absent: %s (%s)", self._last_attempt_path, error
            )
            return None

    def write_last_wifi_attempt(self, attempt: WifiAttempt) -> None:
        payload = {
            "ssid": attempt.ssid,
            "result": attempt.result,
            "timestamp": attempt.timestamp,
            "observed_connection_name": attempt.observed_connection_name,
        }
        try:
            atomic_write_text(self._last_attempt_path, json.dumps(payload))
        except OSError as error:
            logger.error("failed to write state file %s: %s", self._last_attempt_path, error)
            raise

    def read_or_create_initial_signing_key(self) -> str:
        key = self._read_initial_signing_key()
        if key is not None:
            return key

        fresh_key = secrets.token_urlsafe(32)
        payload = json.dumps({"signing_key": fresh_key})
        try:
            create_exclusive_text(self._initial_session_key_path, payload)
            return fresh_key
        except FileExistsError:
            pass

        # A file exists but our read above returned None -- either a
        # concurrent writer won the creation race, or the pre-existing
        # file is genuinely corrupt. Re-read to tell the two apart.
        won = self._read_initial_signing_key()
        if won is not None:
            return won

        # Still unreadable: genuinely corrupt, not a race -- repair in
        # place with atomic_write_text (which overwrites) rather than
        # minting a fresh key every restart that never gets persisted.
        logger.warning(
            "initial session key file is corrupt, repairing with a freshly generated key: %s",
            self._initial_session_key_path,
        )
        atomic_write_text(self._initial_session_key_path, payload)
        return fresh_key

    def discard_initial_signing_key(self) -> None:
        """Delete the initial-mode signing key file, fsyncing the parent directory afterward.

        Without the directory fsync, a power loss right after this call
        returns could lose the unlink itself, resurrecting a key this
        method's caller
        (:func:`~palmimo_portal.core.auth.change_password_from_initial`)
        already told the rest of the system is gone.
        """
        existed = self._initial_session_key_path.exists()
        self._initial_session_key_path.unlink(missing_ok=True)
        if existed:
            fsync_dir(self._state_dir)

    def _read_initial_signing_key(self) -> str | None:
        if not self._initial_session_key_path.is_file():
            return None
        try:
            data: Any = json.loads(self._initial_session_key_path.read_text(encoding="utf-8"))
            key = data["signing_key"] if isinstance(data, dict) else None
        except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as error:
            logger.error(
                "initial session key file is corrupt or unreadable, regenerating: %s (%s)",
                self._initial_session_key_path,
                error,
            )
            return None
        if not isinstance(key, str) or not key:
            logger.error(
                "initial session key file has an unexpected shape, regenerating: %s",
                self._initial_session_key_path,
            )
            return None
        return key

    @staticmethod
    def _parse_update_state(text: str, *, path: Path | None = None) -> UpdateState:
        """Parse ``update.json`` text, raising on any decode, shape, or type problem.

        Not security-bearing -- unlike ``auth.json``, a parse failure here
        is treated as absent by :meth:`read_update_state`, not a distinct
        "corrupt" state. Still validated as strictly, though: a malformed
        ``latest.tag`` would later be handed to ``git``/``uv`` as an update
        target, and a non-numeric ``started_at`` would make
        :func:`~palmimo_portal.core.update.expire_stale_restart` raise on
        every ``GET /update/status`` call. Every field-level problem
        (including an unrecognized ``job.state``/``job.kind``) is treated as
        corrupt -> idle by :meth:`read_update_state` when it propagates.

        An unrecognized ``job.state``/``job.kind`` is the one exception to
        "raise and fall back to idle": handled here directly, salvaging
        ``latest``/``checked_at``/``previous_tag`` (when they validate) and
        resetting only ``job`` to
        :data:`~palmimo_portal.core.update.IDLE_UPDATE_JOB`, logged at
        WARNING. This is what a *downgrade* to an older Portal build looks
        like reading an ``update.json`` a newer build already advanced into
        a state the older build has never heard of -- no reason to also
        throw away a good ``latest`` release record over that.
        """
        data: Any = json.loads(text)
        if not isinstance(data, dict):
            raise TypeError(f"update.json top-level value is a {type(data).__name__}, expected an object")

        latest_data = data.get("latest")
        latest = None
        if latest_data is not None:
            if not isinstance(latest_data, dict):
                raise TypeError("update.json latest must be an object or null")
            tag, name, published_at, html_url = (
                latest_data["tag"],
                latest_data["name"],
                latest_data["published_at"],
                latest_data["html_url"],
            )
            if not all(isinstance(value, str) for value in (tag, name, published_at, html_url)):
                raise TypeError("update.json latest.tag/name/published_at/html_url must be strings")
            if not is_valid_release_tag(tag):
                raise TypeError(f"update.json latest.tag is not a valid release tag: {tag!r}")
            latest = Release(tag=tag, name=name, published_at=published_at, html_url=html_url)

        checked_at = data.get("checked_at")
        _require_optional_type(checked_at, (int, float), "update.json checked_at")
        previous_tag = data.get("previous_tag")
        _require_optional_type(previous_tag, str, "update.json previous_tag")

        job_data = data["job"]
        if not isinstance(job_data, dict):
            raise TypeError("update.json job must be an object")

        job_state = job_data["state"]
        job_kind = job_data.get("kind", "update")
        for field_name in ("target", "step", "error"):
            _require_optional_type(job_data.get(field_name), str, f"update.json job.{field_name}")
        for field_name in ("started_at", "finished_at", "restarting_at"):
            _require_optional_type(job_data.get(field_name), (int, float), f"update.json job.{field_name}")

        if job_state not in _VALID_UPDATE_JOB_STATES or job_kind not in _VALID_UPDATE_JOB_KINDS:
            logger.warning(
                "update.json job has an unrecognized state/kind (state=%r, kind=%r), likely from a newer "
                "Portal build -- salvaging latest/checked_at/previous_tag and resetting the job to idle: %s",
                job_state,
                job_kind,
                path,
            )
            return UpdateState(latest=latest, checked_at=checked_at, previous_tag=previous_tag, job=IDLE_UPDATE_JOB)

        job = UpdateJob(
            state=job_state,
            kind=job_kind,
            target=job_data.get("target"),
            step=job_data.get("step"),
            error=job_data.get("error"),
            started_at=job_data.get("started_at"),
            finished_at=job_data.get("finished_at"),
            # Absent in an `update.json` written before this field existed --
            # tolerated as `None`, the same as every other optional field
            # here (`.get(...)` rather than `[...]`).
            restarting_at=job_data.get("restarting_at"),
        )
        return UpdateState(latest=latest, checked_at=checked_at, previous_tag=previous_tag, job=job)

    def read_update_state(self) -> UpdateState:
        if not self._update_state_path.is_file():
            return IDLE_UPDATE_STATE
        try:
            return self._parse_update_state(
                self._update_state_path.read_text(encoding="utf-8"), path=self._update_state_path
            )
        except (json.JSONDecodeError, KeyError, OSError, TypeError, ValueError) as error:
            logger.warning(
                "update state file is corrupt or unreadable, treating as idle: %s (%s)", self._update_state_path, error
            )
            return IDLE_UPDATE_STATE

    def write_update_state(self, state: UpdateState) -> None:
        payload = {
            "latest": (
                {
                    "tag": state.latest.tag,
                    "name": state.latest.name,
                    "published_at": state.latest.published_at,
                    "html_url": state.latest.html_url,
                }
                if state.latest is not None
                else None
            ),
            "checked_at": state.checked_at,
            "previous_tag": state.previous_tag,
            "job": {
                "state": state.job.state,
                "kind": state.job.kind,
                "target": state.job.target,
                "step": state.job.step,
                "error": state.job.error,
                "started_at": state.job.started_at,
                "finished_at": state.job.finished_at,
                "restarting_at": state.job.restarting_at,
            },
        }
        try:
            atomic_write_text(self._update_state_path, json.dumps(payload))
        except OSError as error:
            logger.error("failed to write state file %s: %s", self._update_state_path, error)
            raise
