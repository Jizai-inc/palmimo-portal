"""In-memory fakes for every port, scriptable for tests.

``PALMIMO_ADAPTERS=fake`` (the default) wires these into the app instead of
the real adapters, so the whole use-case layer can be exercised — in tests
and in ``make dev`` on a machine with no D-Bus or ``authorized_keys`` file —
without touching real hardware or OS state.
"""

from __future__ import annotations

import contextlib
import json
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from palmimo_portal.core.auth import AUTH_LOCK_TIMEOUT_SECONDS
from palmimo_portal.core.platform_update import IDLE_PLATFORM_STATE
from palmimo_portal.core.secrets import normalize_host_owner, validate_secret_name, validate_secret_value
from palmimo_portal.core.sshkey import fingerprint_key, parse_authorized_key
from palmimo_portal.core.update import IDLE_UPDATE_STATE
from palmimo_portal.core.wifi_attempt import resolve_attempt
from palmimo_portal.ports import (
    IDENTITY_UNAVAILABLE,
    AppRefKind,
    AppsLockTimeoutError,
    AppSource,
    AppsState,
    AppsStateFileState,
    AppUnitPort,
    AuthAlreadyExistsError,
    AuthFileState,
    AuthLockTimeoutError,
    AuthState,
    CatalogApp,
    CatalogAsset,
    CatalogCacheState,
    CatalogSource,
    CatalogSourceError,
    ClockPort,
    ConnectionState,
    DiskPort,
    DuplicateKeyError,
    GitCommandError,
    GitCredentialRecord,
    GitPort,
    Identity,
    IdentityStore,
    IdentityUnavailable,
    InstalledVersion,
    JournalEntry,
    JournalInvocation,
    JournalPage,
    JournalPort,
    KeyNotFoundError,
    LastKeyError,
    NetworkPort,
    NotConnectedError,
    PlatformBundleFetchError,
    PlatformBundleSource,
    PlatformCommandError,
    PlatformInstalled,
    PlatformLockTimeoutError,
    PlatformManifest,
    PlatformPort,
    PlatformUpdateState,
    PlatformVerifyDiff,
    Release,
    ReleaseSource,
    ReleaseSourceError,
    RunDirPort,
    RunLockTimeoutError,
    SecretInUseError,
    SecretNotFoundError,
    SecretRecord,
    SecretsStore,
    SshKey,
    SshKeyPort,
    StateStore,
    SyncUnitPort,
    SystemPort,
    UnitStatus,
    UnknownSecretNameError,
    Updater,
    UpdateState,
    UpdateStepError,
    UvPort,
    WifiAttempt,
    WifiNetwork,
    WifiStatus,
)
from palmimo_portal.settings import DEFAULT_REQUIRED_PLATFORM_VERSION


@dataclass
class FakeNetworkPort(NetworkPort):
    """Scriptable :class:`NetworkPort`. Unprovisioned by default.

    Tests drive the state machine by setting ``status`` and
    ``known_networks`` directly, or by calling :meth:`connect`, which
    records the attempt in ``connect_calls`` and applies whatever
    ``next_connect_result`` says (default: succeed and connect).
    """

    status: WifiStatus = field(
        default_factory=lambda: WifiStatus(state=ConnectionState.UNPROVISIONED, ssid=None, ip_address=None)
    )
    scanned_networks: list[WifiNetwork] = field(default_factory=list)
    known_networks: set[str] = field(default_factory=set)
    connect_calls: list[tuple[str, str]] = field(default_factory=list)
    #: SSIDs `forget_current` was asked to forget, including one recorded automatically by
    #: `connect` when already CONNECTED (mirrors ComitupNetworkPort's connect-while-connected rule).
    forget_calls: list[str | None] = field(default_factory=list)
    next_connect_result: WifiStatus | None = None
    raise_on_connect: Exception | None = None  #: makes `connect` raise instead of succeeding
    raise_on_forget: Exception | None = None  #: makes `forget_current` raise instead of succeeding
    raise_on_get_status: Exception | None = None  #: makes `get_status` raise instead of returning `status`
    raise_on_list_networks: Exception | None = None  #: like raise_on_get_status, for GET /wifi/networks
    #: When set, every `get_status` call resolves a pending `last_wifi_attempt` against this
    #: store, mirroring ComitupNetworkPort's real behavior.
    state_store: StateStore | None = None
    clock: Callable[[], float] = field(default=time.time)  #: injectable clock for resolve_attempt
    #: Tracks whether `get_status` has ever been called -- mirrors ComitupNetworkPort's
    #: `_last_logged is None` check, so the *first* call resolves a pending attempt too.
    _observed_before: bool = field(default=False, init=False, repr=False)

    def get_status(self) -> WifiStatus:
        if self.raise_on_get_status is not None:
            raise self.raise_on_get_status
        is_first_observation = not self._observed_before
        self._observed_before = True
        self._resolve_pending_attempt(is_first_observation=is_first_observation)
        return self.status

    def list_networks(self) -> list[WifiNetwork]:
        if self.raise_on_list_networks is not None:
            raise self.raise_on_list_networks
        return list(self.scanned_networks)

    def has_known_networks(self) -> bool:
        return bool(self.known_networks)

    def connect(self, ssid: str, psk: str) -> None:
        if self.status.state is ConnectionState.CONNECTED:
            # Mirrors ComitupNetworkPort.connect: forget the current SSID first, or
            # comitup would short-circuit back to it.
            self.forget_current()
        self.connect_calls.append((ssid, psk))
        if self.raise_on_connect is not None:
            raise self.raise_on_connect
        self.known_networks.add(ssid)
        if self.next_connect_result is not None:
            self.status = self.next_connect_result
        else:
            self.status = WifiStatus(state=ConnectionState.CONNECTING, ssid=ssid, ip_address=None)

    def forget_current(self) -> None:
        # Mirrors ComitupNetworkPort's fresh-read rule: raise rather than pretend to forget
        # when nothing is connected -- the real adapter would otherwise delete comitup's
        # own hotspot profile instead.
        if self.status.state is not ConnectionState.CONNECTED:
            raise NotConnectedError(f"fake network port is not CONNECTED (state={self.status.state!r})")
        current_ssid = self.status.ssid
        self.forget_calls.append(current_ssid)
        if self.raise_on_forget is not None:
            raise self.raise_on_forget
        if current_ssid is not None:
            self.known_networks.discard(current_ssid)
        next_state = ConnectionState.UNPROVISIONED if not self.known_networks else ConnectionState.CONNECTING
        self.status = WifiStatus(state=next_state, ssid=None, ip_address=None)

    def simulate_transition(
        self, state: ConnectionState, ssid: str | None = None, ip_address: str | None = None
    ) -> None:
        """Test-only hook: simulate the adapter observing a new connection state.

        Only updates :attr:`status`, as a real comitup transition would. The *next*
        `get_status` call resolves any pending `last_wifi_attempt`.
        """
        self.status = WifiStatus(state=state, ssid=ssid, ip_address=ip_address)

    def _resolve_pending_attempt(self, *, is_first_observation: bool) -> None:
        if self.state_store is None:
            return
        attempt = self.state_store.read_last_wifi_attempt()
        resolution = resolve_attempt(
            attempt=attempt,
            observed_state=self.status.state,
            is_first_observation=is_first_observation,
            observed_connection_name=self.status.ssid,
            now=self.clock(),
        )
        if resolution is None:
            return
        assert attempt is not None  # resolve_attempt only returns non-None when attempt is not None
        self.state_store.write_last_wifi_attempt(
            WifiAttempt(
                ssid=attempt.ssid,
                result=resolution.result,
                timestamp=self.clock(),
                observed_connection_name=resolution.observed_connection_name,
            )
        )


@dataclass
class FakeSystemPort(SystemPort):
    """Records reboot/shutdown calls instead of touching the real machine."""

    reboot_calls: int = 0
    shutdown_calls: int = 0
    restart_calls: int = 0
    raise_on_reboot: Exception | None = None  #: makes `reboot` raise instead of succeeding
    raise_on_shutdown: Exception | None = None  #: like raise_on_reboot, for POST /system/shutdown
    raise_on_restart_portal: Exception | None = None  #: makes `restart_portal` raise instead of succeeding

    def reboot(self) -> None:
        if self.raise_on_reboot is not None:
            raise self.raise_on_reboot
        self.reboot_calls += 1

    def shutdown(self) -> None:
        if self.raise_on_shutdown is not None:
            raise self.raise_on_shutdown
        self.shutdown_calls += 1

    def restart_portal(self) -> None:
        if self.raise_on_restart_portal is not None:
            raise self.raise_on_restart_portal
        self.restart_calls += 1


@dataclass
class FakeSshKeyPort(SshKeyPort):
    """In-memory :class:`SshKeyPort`, keyed by fingerprint."""

    _keys: dict[str, SshKey] = field(default_factory=dict)
    _raw: dict[str, str] = field(default_factory=dict)

    def list_keys(self) -> list[SshKey]:
        return list(self._keys.values())

    def add_key(self, public_key: str) -> SshKey:
        key_type, comment = parse_authorized_key(public_key)
        fingerprint = fingerprint_key(public_key.strip())
        if fingerprint in self._keys:
            raise DuplicateKeyError(fingerprint)
        key = SshKey(fingerprint=fingerprint, key_type=key_type, comment=comment)
        self._keys[fingerprint] = key
        self._raw[fingerprint] = public_key
        return key

    def delete_key(self, fingerprint: str, *, allow_last: bool = False) -> None:
        if fingerprint not in self._keys:
            raise KeyNotFoundError(fingerprint)
        if len(self._keys) == 1 and not allow_last:
            raise LastKeyError(fingerprint)
        del self._keys[fingerprint]
        del self._raw[fingerprint]


@dataclass
class FakeStateStore(StateStore):
    """In-memory :class:`StateStore`.

    ``auth_corrupt`` is a test-only scripting hook that puts the store into
    :attr:`~palmimo_portal.ports.AuthFileState.CORRUPT` without a real,
    unparseable file on disk -- the real adapter
    (:class:`~palmimo_portal.adapters.state.JsonFileStateStore`) derives the
    same state from ``auth.json``'s actual contents.
    """

    _auth: AuthState | None = None
    _last_attempt: WifiAttempt | None = None
    auth_corrupt: bool = False
    #: Exercises POST /auth/reset's failure path (the reset rate-limit budget must not be
    #: spent when the delete itself fails).
    raise_on_delete_auth: Exception | None = None
    _initial_signing_key: str | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _update_state: UpdateState = field(default_factory=lambda: IDLE_UPDATE_STATE)
    #: Exercises a disk-full write failure (e.g. POST /update/apply returning 500 and
    #: persisting nothing).
    raise_on_write_update_state: Exception | None = None
    _apps_state: AppsState = field(default_factory=AppsState)
    #: Test-only scripting hook, mirroring `auth_corrupt`: puts apps.json into
    #: AppsStateFileState.CORRUPT without a real unparseable file on disk.
    apps_state_corrupt: bool = False
    apps_state_legacy: bool = False
    _apps_state_written: bool = field(default=False, init=False, repr=False)
    _apps_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _run_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _platform_update_state: PlatformUpdateState = field(default_factory=lambda: IDLE_PLATFORM_STATE)
    _platform_lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _catalog_cache: CatalogCacheState = field(
        default_factory=lambda: CatalogCacheState(tag=None, apps=(), fetched_at=None)
    )

    def read_auth(self) -> AuthState | None:
        if self.auth_corrupt:
            return None
        return self._auth

    def auth_state(self) -> AuthFileState:
        if self.auth_corrupt:
            return AuthFileState.CORRUPT
        return AuthFileState.PRESENT if self._auth is not None else AuthFileState.ABSENT

    def create_auth(self, state: AuthState) -> None:
        if self._auth is not None or self.auth_corrupt:
            raise AuthAlreadyExistsError()
        self._auth = state

    def write_auth(self, state: AuthState) -> None:
        self._auth = state
        self.auth_corrupt = False

    def delete_auth(self) -> None:
        with self.lock_auth():
            if self.raise_on_delete_auth is not None:
                raise self.raise_on_delete_auth
            self._auth = None
            self.auth_corrupt = False
            if self._initial_signing_key is not None:
                self._initial_signing_key = secrets.token_urlsafe(32)

    def read_last_wifi_attempt(self) -> WifiAttempt | None:
        return self._last_attempt

    def write_last_wifi_attempt(self, attempt: WifiAttempt) -> None:
        self._last_attempt = attempt

    def read_or_create_initial_signing_key(self) -> str:
        if self._initial_signing_key is None:
            self._initial_signing_key = secrets.token_urlsafe(32)
        return self._initial_signing_key

    def discard_initial_signing_key(self) -> None:
        self._initial_signing_key = None

    @contextlib.contextmanager
    def lock_auth(self) -> Iterator[None]:
        """Bounded `threading.Lock` stand-in for the real adapter's bounded `flock`.

        Mirrors JsonFileStateStore.lock_auth's bounded-wait semantics (same timeout, same
        exception on timeout).
        """
        acquired = self._lock.acquire(timeout=AUTH_LOCK_TIMEOUT_SECONDS)
        if not acquired:
            raise AuthLockTimeoutError()
        try:
            yield
        finally:
            self._lock.release()

    def read_update_state(self) -> UpdateState:
        return self._update_state

    def write_update_state(self, state: UpdateState) -> None:
        if self.raise_on_write_update_state is not None:
            raise self.raise_on_write_update_state
        self._update_state = state

    def read_apps_state(self) -> AppsState:
        if self.apps_state_corrupt or self.apps_state_legacy:
            return AppsState()
        return self._apps_state

    def apps_state_file_state(self) -> AppsStateFileState:
        if self.apps_state_corrupt:
            return AppsStateFileState.CORRUPT
        if self.apps_state_legacy:
            return AppsStateFileState.LEGACY
        return AppsStateFileState.PRESENT if self._apps_state_written else AppsStateFileState.ABSENT

    def write_apps_state(self, state: AppsState) -> None:
        self._apps_state = state
        self._apps_state_written = True
        self.apps_state_corrupt = False
        self.apps_state_legacy = False

    @contextlib.contextmanager
    def lock_apps(self) -> Iterator[None]:
        """Non-blocking `threading.Lock` stand-in, mirroring the real adapter's non-blocking `flock`."""
        if not self._apps_lock.acquire(blocking=False):
            raise AppsLockTimeoutError()
        try:
            yield
        finally:
            self._apps_lock.release()

    @contextlib.contextmanager
    def lock_run(self) -> Iterator[None]:
        """Non-blocking `threading.Lock` stand-in, mirroring the real adapter's non-blocking `flock`."""
        if not self._run_lock.acquire(blocking=False):
            raise RunLockTimeoutError()
        try:
            yield
        finally:
            self._run_lock.release()

    def read_platform_update_state(self) -> PlatformUpdateState:
        return self._platform_update_state

    def write_platform_update_state(self, state: PlatformUpdateState) -> None:
        self._platform_update_state = state

    @contextlib.contextmanager
    def lock_platform(self) -> Iterator[None]:
        """Non-blocking `threading.Lock` stand-in, mirroring the real adapter's non-blocking `flock`."""
        if not self._platform_lock.acquire(blocking=False):
            raise PlatformLockTimeoutError()
        try:
            yield
        finally:
            self._platform_lock.release()

    def read_catalog_cache(self) -> CatalogCacheState:
        return self._catalog_cache

    def write_catalog_cache(self, state: CatalogCacheState) -> None:
        self._catalog_cache = state


@dataclass
class FakeIdentityStore(IdentityStore):
    """Scriptable :class:`IdentityStore`. No identity (DIY/open-setup) by default.

    A test wanting an identity-carrying device sets ``identity`` directly. ``unavailable``
    simulates a transient read failure (e.g. `/boot/firmware` not mounted yet) without a real
    unreadable file on disk -- the real adapter derives the same state from an actual `OSError`.
    """

    identity: Identity | None = None
    unavailable: bool = False

    def read_identity(self) -> Identity | IdentityUnavailable | None:
        if self.unavailable:
            return IDENTITY_UNAVAILABLE
        return self.identity

    def read_identity_uncached(self) -> Identity | IdentityUnavailable | None:
        """Mirrors :meth:`read_identity` -- this fake holds no cache to bypass.

        Exists only to satisfy the full :class:`IdentityStore` protocol; the real cache
        distinction is exercised against `FileIdentityStore` in `tests/test_identity_adapter.py`.
        """
        return self.read_identity()


@dataclass
class FakeReleaseSource(ReleaseSource):
    """Scriptable :class:`ReleaseSource`. Reports no release by default (mirrors ``no_release``)."""

    latest: Release | None = None
    #: Exercises POST /update/check's ReleaseSourceError mapping.
    raise_on_fetch: Exception | None = None
    fetch_calls: int = field(default=0, init=False, repr=False)

    def fetch_latest(self) -> Release:
        self.fetch_calls += 1
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        if self.latest is None:
            raise ReleaseSourceError("no_release", "fake release source has no configured release")
        return self.latest


@dataclass
class FakeUpdater(Updater):
    """Scriptable :class:`Updater`. Reports an untagged checkout by default."""

    installed_version: InstalledVersion = field(default_factory=lambda: InstalledVersion(tag=None, commit="abc123"))
    steps: tuple[str, ...] = ("fetch", "assets", "checkout", "sync", "install-assets")
    fail_at_step: str | None = None  #: when set to one of `steps`, `apply` raises UpdateStepError there
    fail_message: str = "boom"
    apply_calls: list[str] = field(default_factory=list)  #: every tag `apply` was called with, in order

    def installed(self) -> InstalledVersion:
        return self.installed_version

    def apply(self, tag: str, on_step: Callable[[str], None]) -> None:
        self.apply_calls.append(tag)
        for step in self.steps:
            on_step(step)
            if step == self.fail_at_step:
                raise UpdateStepError(step, self.fail_message)
        self.installed_version = InstalledVersion(tag=tag, commit=self.installed_version.commit)


@dataclass
class FakePlatformPort(PlatformPort):
    """Scriptable :class:`PlatformPort`. Records every call so a test can assert none happened.

    Reports the required platform version installed and clean by default -- matches
    ``Settings.required_platform_version``'s own default, so every
    pre-existing test that never mentions the platform channel (app
    install/start, autostart, ...) keeps seeing ``platform_ready`` as
    ``True`` without opting in.
    """

    installed: PlatformInstalled | None = field(
        default_factory=lambda: PlatformInstalled(
            version=DEFAULT_REQUIRED_PLATFORM_VERSION, installed_at="2026-01-01T00:00:00Z", bundle_sha256="0" * 64
        )
    )
    verify_diffs: list[PlatformVerifyDiff] = field(default_factory=list)
    fail_on: str | None = None  #: "verify" / "install" / "record" -- makes that call raise
    fail_message: str = "boom"
    verify_calls: list[Path] = field(default_factory=list)
    install_calls: list[Path] = field(default_factory=list)
    record_calls: list[tuple[Path, str]] = field(default_factory=list)
    #: What `record` sets `installed.version` to -- a test sets this before triggering the job.
    next_version: int | None = None

    def read_installed(self) -> PlatformInstalled | None:
        return self.installed

    def verify(self, bundle_dir: Path) -> list[PlatformVerifyDiff]:
        self.verify_calls.append(bundle_dir)
        if self.fail_on == "verify":
            raise PlatformCommandError("verify", self.fail_message)
        return list(self.verify_diffs)

    def install(self, bundle_dir: Path) -> None:
        self.install_calls.append(bundle_dir)
        if self.fail_on == "install":
            raise PlatformCommandError("install", self.fail_message)

    def record(self, bundle_dir: Path, sha: str) -> None:
        self.record_calls.append((bundle_dir, sha))
        if self.fail_on == "record":
            raise PlatformCommandError("record", self.fail_message)
        self.installed = PlatformInstalled(
            version=self.next_version or 0, installed_at="2026-01-01T00:00:00Z", bundle_sha256=sha
        )


@dataclass
class FakePlatformBundleSource(PlatformBundleSource):
    """Scriptable :class:`PlatformBundleSource`. Writes a minimal fake bundle (``install.sh`` + ``manifest.json``)."""

    manifest: dict[str, Any] = field(default_factory=dict)
    sha: str = "0" * 64
    fail_on: str | None = None  #: "fetch" / "verify_sha" / "extract" -- makes fetch raise there
    fail_message: str = "boom"
    fetch_calls: list[str] = field(default_factory=list)
    #: The ``.manifest.json`` asset's content, keyed same as `manifest` -- `None` (the default)
    #: simulates an older `palmimo-image` release with no manifest asset published yet, so
    #: `fetch_latest_manifest` raises and the caller falls back to `fetch`.
    manifest_asset: dict[str, Any] | None = None
    fetch_manifest_calls: list[str] = field(default_factory=list)

    def fetch_latest_manifest(self, tag: str) -> PlatformManifest:
        self.fetch_manifest_calls.append(tag)
        if self.manifest_asset is None:
            raise PlatformBundleFetchError("fetch_manifest", "no manifest asset configured")
        data = self.manifest_asset
        return PlatformManifest(
            version=int(data["version"]),
            requires_portal=str(data["requires_portal"]),
            restart_portal=bool(data["restart_portal"]),
            summary=str(data["summary"]),
            reflash_required=bool(data.get("reflash_required", False)),
        )

    def fetch(self, tag: str, dest_dir: Path, on_step: Callable[[str], None]) -> tuple[Path, str]:
        self.fetch_calls.append(tag)
        for step in ("fetch", "verify_sha", "extract"):
            on_step(step)
            if step == self.fail_on:
                raise PlatformBundleFetchError(step, self.fail_message)
        bundle_dir = dest_dir / "staging"
        bundle_dir.mkdir(parents=True, exist_ok=True)
        (bundle_dir / "install.sh").write_text("#!/bin/sh\n", encoding="utf-8")
        (bundle_dir / "manifest.json").write_text(json.dumps(self.manifest), encoding="utf-8")
        return bundle_dir, self.sha


@dataclass
class FakeSecretsStore(SecretsStore):
    """In-memory :class:`SecretsStore`, enforcing the same name/value rules as the real adapter."""

    _values: dict[str, tuple[str, float]] = field(default_factory=dict)
    _bindings: dict[str, dict[str, str]] = field(default_factory=dict)
    _git_credentials: dict[str, tuple[str, float]] = field(default_factory=dict)
    #: host_owner -> (rejected_at, rejected_status), mirroring the real adapter's persisted fields.
    _git_credential_rejections: dict[str, tuple[float, int]] = field(default_factory=dict)
    clock: Callable[[], float] = field(default=time.time)

    def list_secrets(self) -> list[SecretRecord]:
        return [SecretRecord(name=name, updated_at=updated_at) for name, (_, updated_at) in self._values.items()]

    def set_secret(self, name: str, value: str) -> None:
        validate_secret_name(name)
        validate_secret_value(value)
        self._values[name] = (value, self.clock())

    def get_secret_value(self, name: str) -> str | None:
        entry = self._values.get(name)
        return entry[0] if entry is not None else None

    def delete_secret(self, name: str) -> None:
        if name not in self._values:
            raise SecretNotFoundError(name)
        users = self.bindings_using(name)
        if users:
            raise SecretInUseError(users)
        del self._values[name]

    def read_bindings(self, app: str) -> dict[str, str]:
        return dict(self._bindings.get(app, {}))

    def write_bindings(self, app: str, bindings: dict[str, str]) -> None:
        unknown = sorted(set(bindings.values()) - set(self._values))
        if unknown:
            raise UnknownSecretNameError(f"unregistered secret name(s): {unknown}")
        self._bindings[app] = dict(bindings)

    def delete_bindings(self, app: str) -> None:
        self._bindings.pop(app, None)

    def bindings_using(self, secret_name: str) -> list[tuple[str, str]]:
        return [
            (app, request_name)
            for app, app_bindings in self._bindings.items()
            for request_name, store_name in app_bindings.items()
            if store_name == secret_name
        ]

    def list_git_credentials(self) -> list[GitCredentialRecord]:
        records = []
        normalized = {
            normalize_host_owner(host_owner): (host_owner, entry) for host_owner, entry in self._git_credentials.items()
        }
        for host_owner, (stored_key, (_, updated_at)) in normalized.items():
            rejection = self._git_credential_rejections.get(stored_key)
            records.append(
                GitCredentialRecord(
                    host_owner=host_owner,
                    updated_at=updated_at,
                    rejected_at=rejection[0] if rejection is not None else None,
                    rejected_status=rejection[1] if rejection is not None else None,
                )
            )
        return records

    def set_git_credential(self, host_owner: str, token: str) -> None:
        host_owner = normalize_host_owner(host_owner)
        validate_secret_value(token)
        for key in list(self._git_credentials):
            if normalize_host_owner(key) == host_owner and key != host_owner:
                self._git_credentials.pop(key)
                self._git_credential_rejections.pop(key, None)
        self._git_credentials[host_owner] = (token, self.clock())
        self._git_credential_rejections.pop(host_owner, None)

    def get_git_credential(self, host_owner: str) -> str | None:
        host_owner = normalize_host_owner(host_owner)
        entry = next(
            (entry for key, entry in self._git_credentials.items() if normalize_host_owner(key) == host_owner), None
        )
        return entry[0] if entry is not None else None

    def delete_git_credential(self, host_owner: str) -> None:
        host_owner = normalize_host_owner(host_owner)
        for key in [key for key in self._git_credentials if normalize_host_owner(key) == host_owner]:
            self._git_credentials.pop(key)
            self._git_credential_rejections.pop(key, None)

    def mark_git_credential_rejected(self, host_owner: str, status: int) -> None:
        host_owner = normalize_host_owner(host_owner)
        if host_owner not in self._git_credentials:
            return
        self._git_credential_rejections[host_owner] = (self.clock(), status)

    def reset(self) -> None:
        self._values.clear()
        self._bindings.clear()
        self._git_credentials.clear()
        self._git_credential_rejections.clear()


@dataclass
class FakeGitPort(GitPort):
    """Scriptable :class:`GitPort`. Every call succeeds with a fake commit SHA by default."""

    next_commit: str = "deadbeef"
    #: (url, ref, ref_kind) -> commit SHA, for `fetch_commit`'s per-app scripting.
    remote_commits: dict[tuple[str, str, str], str] = field(default_factory=dict)
    clone_calls: list[tuple[str, str, str]] = field(default_factory=list)
    clone_options: list[tuple[bool, str | None]] = field(default_factory=list)
    fetch_commit_calls: list[tuple[str, str, str]] = field(default_factory=list)
    raise_on_clone: Exception | None = None
    raise_on_fetch_commit: Exception | None = None
    #: (url, ref, ref_kind) -> exception, for scripting one app's `fetch_commit` to fail
    #: (e.g. a rejected credential) while others in the same sweep keep succeeding.
    raise_on_fetch_commit_for: dict[tuple[str, str, str], Exception] = field(default_factory=dict)
    #: Called with (dest, manifest_text) on a successful clone, so a test can seed the
    #: cloned tree's palmimo.toml/pyproject.toml without a real git binary.
    on_clone: Callable[[Path, str, str, str], None] | None = None

    def clone_shallow(
        self,
        url: str,
        ref: str,
        ref_kind: AppRefKind,
        dest: Path,
        *,
        env: Mapping[str, str] | None = None,
        blobless: bool = False,
        sparse_subdir: str | None = None,
    ) -> str:
        self.clone_calls.append((url, ref, ref_kind))
        self.clone_options.append((blobless, sparse_subdir))
        if self.raise_on_clone is not None:
            raise self.raise_on_clone
        if self.on_clone is not None:
            self.on_clone(dest, url, ref, ref_kind)
        return self.remote_commits.get((url, ref, ref_kind), self.next_commit)

    def fetch_commit(self, url: str, ref: str, ref_kind: AppRefKind, *, env: Mapping[str, str] | None = None) -> str:
        self.fetch_commit_calls.append((url, ref, ref_kind))
        if (url, ref, ref_kind) in self.raise_on_fetch_commit_for:
            raise self.raise_on_fetch_commit_for[(url, ref, ref_kind)]
        if self.raise_on_fetch_commit is not None:
            raise self.raise_on_fetch_commit
        if (url, ref, ref_kind) not in self.remote_commits:
            raise GitCommandError(f"fake git port has no configured commit for {(url, ref, ref_kind)!r}")
        return self.remote_commits[(url, ref, ref_kind)]


@dataclass
class FakeUvPort(UvPort):
    """Scriptable :class:`UvPort`. Reports a fixed version string by default."""

    version_value: str = "uv 0.0.0-fake"

    def version(self) -> str:
        return self.version_value


@dataclass
class FakeSyncUnitPort(SyncUnitPort):
    """Scriptable :class:`SyncUnitPort`. Every instance finishes ``success``/exit 0 by default.

    ``statuses``/``journal_tails`` are keyed by instance (the ``.staging/<id>/`` directory
    name) -- a test scripts one instance to fail by setting a non-``"success"`` result and,
    optionally, journal lines :meth:`~palmimo_portal.core.apps_jobs.AppsJobContext` would read
    back through the (separately faked) :class:`~palmimo_portal.ports.JournalPort`.
    """

    statuses: dict[str, UnitStatus] = field(default_factory=dict)
    default_status: UnitStatus = field(
        default_factory=lambda: UnitStatus(
            active_state="inactive", sub_state="dead", result="success", exec_main_status=0
        )
    )
    start_calls: list[str] = field(default_factory=list)
    wait_calls: list[str] = field(default_factory=list)
    stop_calls: list[str] = field(default_factory=list)
    raise_on_start: Exception | None = None
    raise_on_wait: Exception | None = None
    #: Root the real ``app-sync`` helper reads ``<instance>/sync.json`` under (``ctx.staging_dir``).
    #: ``None`` unless a test needs to exercise the unit's actual work (e.g. purge mode).
    staging_dir: Path | None = None
    #: Every ``sync.json`` this fake has read, keyed by instance -- lets a test assert on a
    #: request's content after `purge_path` has already deleted the staging directory it lived in.
    sync_specs: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Absolute paths (as strings) a purge request must fail to remove, simulating a
    #: file/directory `palmimo-app` owns that Portal's own uid cannot delete even through the
    #: privileged sync unit.
    undeletable_paths: set[str] = field(default_factory=set)
    #: Instance names a test scripts as already active -- simulates a unit left running by a
    #: process that died mid-job, for :meth:`list_active_instances`.
    active_instances: set[str] = field(default_factory=set)

    def start(self, instance: str) -> None:
        self.start_calls.append(instance)
        if self.raise_on_start is not None:
            raise self.raise_on_start

    def wait(self, instance: str, timeout_s: float) -> UnitStatus:
        self.wait_calls.append(instance)
        if self.raise_on_wait is not None:
            raise self.raise_on_wait
        status = self.statuses.get(instance, self.default_status)
        if status.result == "success" and status.exec_main_status == 0:
            self._simulate_unit_work(instance)
        return status

    def _simulate_unit_work(self, instance: str) -> None:
        """Stand in for the real ``app-sync`` helper: read ``<instance>/sync.json`` and, if it's
        a purge request, remove the target -- unless the test scripted that target undeletable.
        A no-op for a plain dependency-sync request (no ``purge`` key) and when `staging_dir`
        is unset (most tests never exercise the unit's actual work)."""
        if self.staging_dir is None:
            return
        sync_path = self.staging_dir / instance / "sync.json"
        try:
            spec = json.loads(sync_path.read_text(encoding="utf-8"))
        except OSError:
            return
        self.sync_specs[instance] = spec
        purge = spec.get("purge")
        if not isinstance(purge, str) or purge in self.undeletable_paths:
            return
        path = Path(purge)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists() or path.is_symlink():
            path.unlink(missing_ok=True)

    def stop(self, instance: str) -> None:
        self.stop_calls.append(instance)
        self.active_instances.discard(instance)

    def list_active_instances(self) -> list[str]:
        return sorted(self.active_instances)


@dataclass
class FakeDiskPort(DiskPort):
    """Scriptable :class:`DiskPort`. Reports abundant free space by default."""

    free_bytes_value: int = 10 * 1024 * 1024 * 1024
    #: Set to simulate `apps_dir` not existing yet (`os.statvfs` raising `OSError`).
    raise_on_free_bytes: OSError | None = None

    def free_bytes(self, path: Path) -> int:
        if self.raise_on_free_bytes is not None:
            raise self.raise_on_free_bytes
        return self.free_bytes_value


@dataclass
class FakeClockPort(ClockPort):
    """Scriptable :class:`ClockPort`. Starts at monotonic 0.0, NTP-synchronized, unless scripted otherwise.

    ``monotonic_value`` is a plain field a test advances directly (``clock.monotonic_value +=
    300.0``) -- there is no real clock underneath to fast-forward.
    """

    monotonic_value: float = 0.0
    synchronized: bool = True

    def monotonic(self) -> float:
        return self.monotonic_value

    def ntp_synchronized(self) -> bool:
        return self.synchronized


@dataclass
class FakeCatalogSource(CatalogSource):
    """Scriptable :class:`CatalogSource`. Reports no catalog (``CatalogSourceError``) by default."""

    asset: CatalogAsset | None = None
    raise_on_fetch: Exception | None = None
    fetch_calls: int = field(default=0, init=False, repr=False)

    def fetch(self) -> CatalogAsset:
        self.fetch_calls += 1
        if self.raise_on_fetch is not None:
            raise self.raise_on_fetch
        if self.asset is None:
            raise CatalogSourceError("fake catalog source has no configured asset")
        return self.asset


def make_catalog_app(
    name: str = "palmimo-teleop", *, manifest: str | None = None, commit: str = "deadbeef", subdir: str | None = None
) -> CatalogApp:
    """Build a minimal, valid :class:`CatalogApp` for scripting :class:`FakeCatalogSource`."""
    return CatalogApp(
        name=name,
        description="A teleop app.",
        source=AppSource(
            type="git",
            url="https://github.com/Jizai-inc/palmimo-devkit",
            ref_kind="tag",
            ref="v1.0.0",
            commit=commit,
            manifest=manifest,
            subdir=subdir,
        ),
        env={},
        devices=("camera",),
    )


def make_wifi_attempt(ssid: str, result: str) -> WifiAttempt:
    """Build a :class:`WifiAttempt` timestamped at call time."""
    return WifiAttempt(ssid=ssid, result=result, timestamp=time.time())


@dataclass
class FakeAppUnitPort(AppUnitPort):
    """Scriptable :class:`AppUnitPort`. Every unit starts ``inactive``/``success`` and idle.

    ``statuses`` is keyed by app name (not unit string) -- tests set it
    directly to script :meth:`status`'s next answer, mirroring
    :class:`FakeNetworkPort`'s ``status`` field.
    """

    statuses: dict[str, UnitStatus] = field(default_factory=dict)
    default_status: UnitStatus = field(
        default_factory=lambda: UnitStatus(
            active_state="inactive", sub_state="dead", result="success", exec_main_status=0
        )
    )
    device_allow_calls: list[tuple[str, list[str]]] = field(default_factory=list)
    start_calls: list[str] = field(default_factory=list)
    stop_calls: list[str] = field(default_factory=list)
    raise_on_set_device_allow: Exception | None = None
    raise_on_start: Exception | None = None
    raise_on_stop: Exception | None = None
    #: App names `start` was called for and not yet `stop`-ped -- backs `list_active_app_units`.
    _active: set[str] = field(default_factory=set, init=False, repr=False)

    def set_device_allow(self, name: str, specs: list[str]) -> None:
        self.device_allow_calls.append((name, list(specs)))
        if self.raise_on_set_device_allow is not None:
            raise self.raise_on_set_device_allow

    def start(self, name: str) -> None:
        self.start_calls.append(name)
        if self.raise_on_start is not None:
            raise self.raise_on_start
        self._active.add(name)
        self.statuses.setdefault(
            name, UnitStatus(active_state="activating", sub_state="start", result="success", exec_main_status=0)
        )

    def stop(self, name: str) -> None:
        self.stop_calls.append(name)
        if self.raise_on_stop is not None:
            raise self.raise_on_stop
        self._active.discard(name)
        self.statuses[name] = UnitStatus(
            active_state="inactive", sub_state="dead", result="success", exec_main_status=0
        )

    def status(self, name: str) -> UnitStatus:
        return self.statuses.get(name, self.default_status)

    def list_active_app_units(self) -> list[str]:
        return sorted(
            name
            for name in self._active
            if self.statuses.get(name, self.default_status).active_state in ("active", "activating", "deactivating")
        )

    def simulate_active_state(self, name: str, status: UnitStatus) -> None:
        """Test-only hook: set `name`'s status directly, adding it to the active set when appropriate."""
        self.statuses[name] = status
        if status.active_state in ("active", "activating", "deactivating"):
            self._active.add(name)
        else:
            self._active.discard(name)


@dataclass
class FakeRunDirPort(RunDirPort):
    """In-memory :class:`RunDirPort`, keyed by app name."""

    written: dict[str, dict[str, Any]] = field(default_factory=dict)
    raise_on_write: Exception | None = None

    def write(self, name: str, *, env: dict[str, str], argv: list[str], cwd: str, project: str) -> None:
        if self.raise_on_write is not None:
            raise self.raise_on_write
        self.written[name] = {"env": dict(env), "argv": list(argv), "cwd": cwd, "project": project}

    def remove(self, name: str) -> None:
        self.written.pop(name, None)

    def remove_all(self) -> None:
        self.written.clear()


@dataclass
class FakeJournalPort(JournalPort):
    """Scriptable :class:`JournalPort`. Readable, with no entries, by default."""

    entries_by_unit: dict[str, list[JournalEntry]] = field(default_factory=dict)
    readable: bool = True

    def read(self, unit: str, *, cursor: str | None, lines: int, invocation: str | None = None) -> JournalPage:
        all_entries = self.entries_by_unit.get(unit, [])
        entries = (
            [entry for entry in all_entries if entry.invocation_id == invocation]
            if invocation is not None
            else all_entries
        )
        start = int(cursor) if cursor is not None else 0
        page = entries[start : start + lines]
        next_cursor = str(start + len(page)) if start + len(page) < len(entries) else None
        invocations: dict[str, float | None] = {}
        for entry in all_entries:
            if entry.invocation_id is not None:
                invocations.setdefault(entry.invocation_id, entry.timestamp)
        # Newest-appearance-first, matching JournalctlPort._list_invocations -- see its
        # docstring for why appearance order, not started_at, is the sort key.
        starts = [JournalInvocation(id=id, started_at=started_at) for id, started_at in invocations.items()]
        starts.reverse()
        return JournalPage(entries=page, next_cursor=next_cursor, invocations=starts[:20])

    def can_read(self) -> bool:
        return self.readable


@dataclass(frozen=True)
class FakeAdapterBundle:
    """The same ports as :class:`~palmimo_portal.wiring.AdapterBundle`, typed to the concrete fakes.

    `AdapterBundle`'s own fields are typed to the port *protocols*, correct for production code
    but too narrow for a test that pokes fake-only attributes (`adapters.network.known_networks`,
    `adapters.updater.fail_at_step`). Tests build a Portal with `settings.adapters == "fake"`
    (the suite's default), so this type lets the test suite say the concrete fakes are there.
    """

    network: FakeNetworkPort
    system: FakeSystemPort
    ssh_keys: FakeSshKeyPort
    state: FakeStateStore
    identity: FakeIdentityStore
    releases: FakeReleaseSource
    updater: FakeUpdater
    secrets: FakeSecretsStore
    git: FakeGitPort
    uv: FakeUvPort
    sync_unit: FakeSyncUnitPort
    disk: FakeDiskPort
    app_unit: FakeAppUnitPort
    run_dir: FakeRunDirPort
    journal: FakeJournalPort
    platform: FakePlatformPort
    platform_bundle: FakePlatformBundleSource
    platform_releases: FakeReleaseSource
    clock: FakeClockPort
    catalog: FakeCatalogSource
