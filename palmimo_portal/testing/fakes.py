"""In-memory fakes for every port, scriptable for tests.

``PALMIMO_ADAPTERS=fake`` (the default) wires these into the app instead of
the real adapters, so the whole use-case layer can be exercised — in tests
and in ``make dev`` on a machine with no D-Bus or ``authorized_keys`` file —
without touching real hardware or OS state.
"""

from __future__ import annotations

import contextlib
import secrets
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field

from palmimo_portal.core.auth import AUTH_LOCK_TIMEOUT_SECONDS
from palmimo_portal.core.sshkey import fingerprint_key, parse_authorized_key
from palmimo_portal.core.update import IDLE_UPDATE_STATE
from palmimo_portal.core.wifi_attempt import resolve_attempt
from palmimo_portal.ports import (
    IDENTITY_UNAVAILABLE,
    AuthAlreadyExistsError,
    AuthFileState,
    AuthLockTimeoutError,
    AuthState,
    ConnectionState,
    DuplicateKeyError,
    Identity,
    IdentityStore,
    IdentityUnavailable,
    InstalledVersion,
    KeyNotFoundError,
    LastKeyError,
    NetworkPort,
    NotConnectedError,
    Release,
    ReleaseSource,
    ReleaseSourceError,
    SshKey,
    SshKeyPort,
    StateStore,
    SystemPort,
    Updater,
    UpdateState,
    UpdateStepError,
    WifiAttempt,
    WifiNetwork,
    WifiStatus,
)


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
    forget_calls: list[str | None] = field(default_factory=list)
    """SSIDs :meth:`forget_current` was asked to forget, including one recorded
    automatically by :meth:`connect` when already CONNECTED (mirrors
    :class:`~palmimo_portal.adapters.comitup.ComitupNetworkPort`'s connect-while-connected rule)."""
    next_connect_result: WifiStatus | None = None
    raise_on_connect: Exception | None = None
    """When set, :meth:`connect` raises this instead of succeeding."""
    raise_on_forget: Exception | None = None
    """When set, :meth:`forget_current` raises this instead of succeeding."""
    raise_on_get_status: Exception | None = None
    """When set, :meth:`get_status` raises this instead of returning ``status``."""
    raise_on_list_networks: Exception | None = None
    """Like :attr:`raise_on_get_status`, for ``GET /wifi/networks``."""
    state_store: StateStore | None = None
    """When set, every :meth:`get_status` call resolves a pending
    ``last_wifi_attempt`` against this store, mirroring
    :class:`~palmimo_portal.adapters.comitup.ComitupNetworkPort`'s real behavior."""
    clock: Callable[[], float] = field(default=time.time)
    """Injectable clock for the ``last_wifi_attempt`` resolution logic
    (:func:`~palmimo_portal.core.wifi_attempt.resolve_attempt`)."""
    _observed_before: bool = field(default=False, init=False, repr=False)
    """Tracks whether :meth:`get_status` has ever been called -- mirrors
    :class:`~palmimo_portal.adapters.comitup.ComitupNetworkPort`'s
    ``_last_logged is None`` check, so the *first* call resolves a pending
    attempt too."""

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
            # Mirrors ComitupNetworkPort.connect's connect-while-connected
            # rule: forget the current SSID first, or comitup would
            # short-circuit back to it.
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
        # Mirrors ComitupNetworkPort.forget_current's fresh-read rule: must
        # raise rather than pretend to forget when nothing is connected --
        # the real adapter would otherwise delete comitup's own hotspot
        # profile instead.
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

        Only updates :attr:`status`, exactly as a real comitup transition
        would. The *next* :meth:`get_status` call is what actually resolves
        a pending ``last_wifi_attempt`` -- both this fake and the real
        adapter funnel that decision through
        :func:`~palmimo_portal.core.wifi_attempt.resolve_attempt`.
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
    raise_on_reboot: Exception | None = None
    """When set, :meth:`reboot` raises this instead of succeeding."""
    raise_on_shutdown: Exception | None = None
    """Like :attr:`raise_on_reboot`, for ``POST /system/shutdown``."""
    raise_on_restart_portal: Exception | None = None
    """When set, :meth:`restart_portal` raises this instead of succeeding."""

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
    raise_on_delete_auth: Exception | None = None
    """When set, :meth:`delete_auth` raises this instead of succeeding --
    exercises ``POST /auth/reset``'s failure path (the reset rate-limit
    budget must not be spent when the delete itself fails)."""
    _initial_signing_key: str | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)
    _update_state: UpdateState = field(default_factory=lambda: IDLE_UPDATE_STATE)
    raise_on_write_update_state: Exception | None = None
    """When set, :meth:`write_update_state` raises this instead of
    succeeding -- exercises a disk-full write failure (e.g.
    ``POST /update/apply`` returning 500 and persisting nothing)."""

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
        """In-memory stand-in for the real adapter's bounded ``flock`` -- a bounded :class:`threading.Lock`.

        Mirrors :class:`~palmimo_portal.adapters.state.JsonFileStateStore.lock_auth`'s
        bounded-wait semantics (same timeout, same exception on timeout).

        Raises:
            AuthLockTimeoutError: the lock could not be acquired within
                :data:`~palmimo_portal.core.auth.AUTH_LOCK_TIMEOUT_SECONDS`.
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


@dataclass
class FakeIdentityStore(IdentityStore):
    """Scriptable :class:`IdentityStore`. No identity (DIY/open-setup) by default.

    A test that wants an identity-carrying device sets ``identity``
    directly, e.g. ``adapters.identity.identity = Identity(device_id=...,
    initial_password_hash=hash_password("sticker-password"))``.

    ``unavailable`` is a test-only scripting hook that simulates a transient
    read failure (e.g. ``/boot/firmware`` not mounted yet) without a real,
    unreadable file on disk. The real adapter
    (:class:`~palmimo_portal.adapters.identity.FileIdentityStore`) derives
    the same state from an actual :class:`OSError`.
    """

    identity: Identity | None = None
    unavailable: bool = False

    def read_identity(self) -> Identity | IdentityUnavailable | None:
        if self.unavailable:
            return IDENTITY_UNAVAILABLE
        return self.identity

    def read_identity_uncached(self) -> Identity | IdentityUnavailable | None:
        """Mirrors :meth:`read_identity` -- this fake holds no cache to bypass.

        Exists only so this fake satisfies the full :class:`IdentityStore`
        protocol; the real distinction is exercised against
        :class:`~palmimo_portal.adapters.identity.FileIdentityStore` in
        ``tests/test_identity_adapter.py``.
        """
        return self.read_identity()


@dataclass
class FakeReleaseSource(ReleaseSource):
    """Scriptable :class:`ReleaseSource`. Reports no release by default (mirrors ``no_release``)."""

    latest: Release | None = None
    raise_on_fetch: Exception | None = None
    """When set, :meth:`fetch_latest` raises this instead of returning
    :attr:`latest` -- exercises ``POST /update/check``'s
    :class:`~palmimo_portal.ports.ReleaseSourceError` mapping."""
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
    fail_at_step: str | None = None
    """When set to one of :attr:`steps`, :meth:`apply` raises
    :class:`~palmimo_portal.ports.UpdateStepError` at that step."""
    fail_message: str = "boom"
    apply_calls: list[str] = field(default_factory=list)
    """Every ``tag`` :meth:`apply` was called with, in order."""
    apply_repair_dirty_calls: list[bool] = field(default_factory=list)
    """Every ``repair_dirty`` :meth:`apply` was called with, in order --
    parallel to :attr:`apply_calls`, so a test can assert the attribution
    rule (``core/update.should_repair_dirty_checkout``) actually reached
    the updater for a given call."""

    def installed(self) -> InstalledVersion:
        return self.installed_version

    def apply(self, tag: str, on_step: Callable[[str], None], *, repair_dirty: bool = False) -> None:
        self.apply_calls.append(tag)
        self.apply_repair_dirty_calls.append(repair_dirty)
        for step in self.steps:
            on_step(step)
            if step == self.fail_at_step:
                raise UpdateStepError(step, self.fail_message)
        self.installed_version = InstalledVersion(tag=tag, commit=self.installed_version.commit)


def make_wifi_attempt(ssid: str, result: str) -> WifiAttempt:
    """Build a :class:`WifiAttempt` timestamped at call time."""
    return WifiAttempt(ssid=ssid, result=result, timestamp=time.time())


@dataclass(frozen=True)
class FakeAdapterBundle:
    """The same seven ports as :class:`~palmimo_portal.wiring.AdapterBundle`, typed to the concrete fakes.

    ``AdapterBundle``'s own fields are typed to the port *protocols*
    (``NetworkPort``, ``SystemPort``, ...), so code holding one statically
    sees only the protocol's members -- correct for production code, which
    must not depend on which adapter is wired in, but too narrow for a test
    that pokes fake-only attributes and controls (``adapters.network.known_networks``,
    ``adapters.updater.fail_at_step``, and so on). Tests build a Portal with
    ``settings.adapters == "fake"`` (the suite's default -- see
    ``tests/conftest.py``), so the objects behind ``request.app.state.adapters``
    really are these concrete fakes; this type lets the test suite say so.
    """

    network: FakeNetworkPort
    system: FakeSystemPort
    ssh_keys: FakeSshKeyPort
    state: FakeStateStore
    identity: FakeIdentityStore
    releases: FakeReleaseSource
    updater: FakeUpdater
