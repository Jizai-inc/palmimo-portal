"""Port definitions (:class:`typing.Protocol`) for the Palmimo Portal.

A port is the seam between the use-case layer (``core/``) and the outside
world. ``core/`` depends only on the protocols defined here; the concrete
implementations — real ones that touch the filesystem or D-Bus, and fakes
that hold everything in memory — live in ``adapters/`` and ``testing/``
respectively. Nothing outside this module and ``adapters/`` should need to
know which concrete class backs a port.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal, Protocol


class ConnectionState(StrEnum):
    """The Wi-Fi state machine, mirroring comitup's: no known network yet
    (``UNPROVISIONED``), mid-attempt (``CONNECTING``), or on the home LAN
    (``CONNECTED``)."""

    UNPROVISIONED = "unprovisioned"
    CONNECTING = "connecting"
    CONNECTED = "connected"


@dataclass(frozen=True)
class WifiStatus:
    """The current Wi-Fi connection state, as reported by :class:`NetworkPort`."""

    state: ConnectionState
    ssid: str | None
    ip_address: str | None


@dataclass(frozen=True)
class WifiNetwork:
    """One network from a nearby-SSID scan."""

    ssid: str
    signal: int
    secured: bool


@dataclass(frozen=True)
class WifiAttempt:
    """The most recent Wi-Fi connect attempt, persisted by :class:`StateStore`.

    Read back by ``GET /api/v1/system/status`` so a client that reconnects
    to the setup AP after a failed attempt can be told why, without a
    WebSocket or SSE channel (see the technical design's "AP disconnection
    asymmetry" section).
    """

    ssid: str
    result: str
    timestamp: float
    observed_connection_name: str | None = None
    """The connection/AP name comitup was actually observed on when this
    attempt resolved, from :mod:`palmimo_portal.core.wifi_attempt`. ``None``
    while the attempt is still ``"attempting"``, or when the real adapter
    reported no name. Distinct from ``ssid`` (the network the client *asked*
    to connect to) because comitup can settle onto a different network than
    the one just attempted -- carrying both lets the UI tell them apart."""


class NetworkPort(Protocol):
    """Wi-Fi state and control, backed by comitup in the real adapter.

    See :class:`~palmimo_portal.adapters.comitup.ComitupNetworkPort`.
    """

    def get_status(self) -> WifiStatus:
        """Return the current connection state."""
        ...

    def list_networks(self) -> list[WifiNetwork]:
        """Return the most recent nearby-SSID scan."""
        ...

    def has_known_networks(self) -> bool:
        """Report whether any network has ever been configured.

        Used by :mod:`palmimo_portal.core.provisioning` alongside
        :meth:`get_status`: a device that is currently disconnected but has
        a known network on file is provisioned, not out-of-box.
        """
        ...

    def connect(self, ssid: str, psk: str) -> None:
        """Start a connection attempt.

        Returns immediately — the attempt happens asynchronously and its
        result is read back later via :class:`StateStore`, never through
        this call's return value (see :class:`WifiAttempt`).
        """
        ...

    def forget_current(self) -> None:
        """Delete the currently connected network's saved profile and drop the connection.

        comitup falls back to HOTSPOT (or another known network, if any). Returns immediately.

        Raises:
            AdapterUnavailableError: like the other calls, when the backend cannot be reached.
            NotConnectedError: the current state is not CONNECTED, checked with a fresh read
                immediately before deciding -- never a cached value. While in HOTSPOT,
                ``delete_connection()`` deletes the NetworkManager profile of the *active*
                SSID on the link device, i.e. comitup's own hotspot profile, not a
                home-network one.
        """
        ...


class SystemPort(Protocol):
    """Power operations, backed by systemd/logind in the real adapter.

    See :class:`~palmimo_portal.adapters.systemd.SystemdSystemPort`.
    """

    def reboot(self) -> None:
        """Reboot the machine."""
        ...

    def shutdown(self) -> None:
        """Shut the machine down safely."""
        ...

    def restart_portal(self) -> None:
        """Restart the Portal's own systemd unit (``systemd1`` ``Manager.RestartUnit``).

        Called by :class:`~palmimo_portal.core.update_runner.UpdateRunner`
        once :class:`Updater.apply` succeeds, so the freshly ``uv sync``'d
        code actually starts running.

        Raises:
            AdapterUnavailableError: the real adapter's D-Bus call to
                systemd timed out or failed.
        """
        ...


class AdapterUnavailableError(Exception):
    """Raised by a real :class:`NetworkPort`/:class:`SystemPort` when its OS backend cannot be reached.

    Covers a D-Bus call that times out or fails even after the adapter's own
    reconnect-and-retry (see :mod:`palmimo_portal.adapters.dbus_support`) --
    comitup or logind is not running, the system bus itself is unreachable,
    or a call simply took too long.

    Distinct from a bare :class:`Exception` so ``api/`` can translate it
    into a 503 ``*_backend_unavailable`` error envelope instead of the
    generic 500 ``internal_error``. ``code`` is the same snake_case i18n
    key the error envelope carries (e.g. ``"network_backend_unavailable"``),
    so ``api/`` does not have to hardcode a mapping from adapter identity to
    error code.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class NotConnectedError(Exception):
    """Raised by :meth:`NetworkPort.forget_current` when the device is not currently CONNECTED.

    ``api/wifi.py``'s ``DELETE /wifi/connection`` translates this to 409
    ``wifi_not_connected`` rather than letting the adapter call
    ``delete_connection()`` on a device not actually connected to anything --
    while in HOTSPOT, that call deletes the NetworkManager profile of the
    *active* SSID on the link device, which in that state is comitup's own
    hotspot profile, not a home-network one. Both the real adapter and
    :class:`~palmimo_portal.testing.fakes.FakeNetworkPort` check a freshly
    read state immediately before deciding whether to raise this -- never a
    cached value.
    """


@dataclass(frozen=True)
class SshKey:
    """One entry from the managed ``authorized_keys`` file."""

    fingerprint: str
    key_type: str
    comment: str


class DuplicateKeyError(Exception):
    """Raised by :meth:`SshKeyPort.add_key` when the key is already registered."""


class InvalidKeyFormatError(Exception):
    """Raised by :meth:`SshKeyPort.add_key` when the given text is not a public key."""


class KeyNotFoundError(Exception):
    """Raised by :meth:`SshKeyPort.delete_key` when no key has the given fingerprint."""


class LastKeyError(Exception):
    """Raised by :meth:`SshKeyPort.delete_key` when deleting the last key without ``allow_last``.

    The OS user would otherwise be left with no way back in over SSH. The
    invariant is enforced inside the delete operation itself (not by the
    caller checking :meth:`SshKeyPort.list_keys` first) so two concurrent
    deletes cannot each observe two keys and both proceed, emptying the file.
    """


class SshKeysLockTimeoutError(Exception):
    """Raised by :class:`~palmimo_portal.adapters.ssh_keys.AuthorizedKeysSshKeyPort` when its lock cannot be acquired in time.

    Mirrors :class:`AuthLockTimeoutError`: a bounded wait on the
    ``authorized_keys`` lockfile means a stuck contender cannot hang every
    other key-management request indefinitely -- ``api/`` translates this
    into 409 ``ssh_keys_busy``.
    """


class SshKeyPort(Protocol):
    """Reads and edits the OS user's ``authorized_keys`` file."""

    def list_keys(self) -> list[SshKey]:
        """Return every registered key."""
        ...

    def add_key(self, public_key: str) -> SshKey:
        """Validate, fingerprint, and append a public key line.

        Raises:
            InvalidKeyFormatError: ``public_key`` is not a well-formed
                ``authorized_keys`` line.
            DuplicateKeyError: the key is already registered.
            SshKeysLockTimeoutError: the ``authorized_keys`` lock could not
                be acquired in time.
        """
        ...

    def delete_key(self, fingerprint: str, *, allow_last: bool = False) -> None:
        """Remove the key with the given fingerprint.

        Args:
            fingerprint: The fingerprint of the key to remove.
            allow_last: Must be ``True`` to remove the last remaining key.

        Raises:
            KeyNotFoundError: no key has that fingerprint.
            LastKeyError: it is the last remaining key and ``allow_last``
                is ``False``.
            SshKeysLockTimeoutError: the ``authorized_keys`` lock could not
                be acquired in time.
        """
        ...


@dataclass(frozen=True)
class AuthState:
    """Persisted authentication material: the password hash and session signing key."""

    password_hash: str
    signing_key: str


class AuthFileState(StrEnum):
    """The three states ``auth.json`` can be in, as classified by :meth:`StateStore.auth_state`.

    Distinguishing ``CORRUPT`` from ``ABSENT`` matters for security, not
    just diagnostics: a file that exists but cannot be parsed must never be
    treated the same as "no password has ever been set" -- doing so would
    reopen the unauthenticated first-time-setup endpoint on a device that
    already has an owner.
    """

    ABSENT = "absent"
    """No ``auth.json`` file exists yet -- the out-of-box state. Setup is allowed."""

    PRESENT = "present"
    """``auth.json`` exists and parses. Normal operation."""

    CORRUPT = "corrupt"
    """``auth.json`` exists but is unreadable or unparseable. Setup and login
    both refuse with 409 ``auth_state_corrupt`` until an operator deletes
    the file (over SSH) to return to :attr:`ABSENT` -- this module never
    deletes it automatically."""


class AuthAlreadyExistsError(Exception):
    """Raised by :meth:`StateStore.create_auth` when ``auth.json`` already exists.

    Distinct from the password-setup-layer's ``PasswordAlreadySetError``:
    this is the low-level, filesystem-race-safe signal that
    :func:`palmimo_portal.core.auth.setup_password` catches and translates.
    """


class AuthLockTimeoutError(Exception):
    """Raised by :meth:`StateStore.lock_auth` when the lock could not be acquired in time.

    A bounded wait, rather than blocking forever, means a stuck contender
    cannot hang every other password-change request indefinitely -- ``api/``
    translates this into 409 ``auth_change_in_progress``.
    """


@dataclass(frozen=True)
class Identity:
    """The manufacturing-written identity of this physical device.

    Present only on a device Jizai provisioned before shipping (see
    palmimo-portal.md's cross-cutting decision 1): the individual number
    printed on the sticker (``device_id``) and an argon2id hash of the
    random password printed alongside it. Absent on a DIY, self-flashed
    image, which falls back to the legacy open first-time-setup flow (see
    :class:`~palmimo_portal.core.identity.PortalAuthState`).
    """

    device_id: str
    initial_password_hash: str


class IdentityUnavailable(StrEnum):
    """Sentinel :meth:`IdentityStore.read_identity` returns for a transient/unexpected read failure.

    Distinct from both a successfully parsed :class:`Identity` and clean
    absence (``None``, "there is no identity file"): ``/boot/firmware``
    mounts separately from the Portal's own filesystem, so an ``OSError``
    reading the identity file before that mount is ready must not be
    mistaken for "this SD card was hand-flashed with no identity file" --
    doing so would let a sticker/OEM device be misclassified as
    :attr:`~palmimo_portal.core.identity.PortalAuthState.OPEN_SETUP`
    (claimable by anyone) for as long as the mount takes to appear.
    """

    UNAVAILABLE = "unavailable"


IDENTITY_UNAVAILABLE = IdentityUnavailable.UNAVAILABLE


class IdentityStore(Protocol):
    """Reads the manufacturing-written identity file. The Portal never writes it.

    Backed by :class:`~palmimo_portal.adapters.identity.FileIdentityStore`
    in the real adapter, which reads ``PALMIMO_IDENTITY_FILE``
    (default ``/boot/firmware/palmimo-identity.json``).
    """

    def read_identity(self) -> Identity | IdentityUnavailable | None:
        """Return the device identity, ``None`` for clean absence, or :data:`IDENTITY_UNAVAILABLE`.

        A malformed (but present) identity file is treated the same as an
        absent one (unlike ``auth.json``, which distinguishes
        :attr:`AuthFileState.CORRUPT` from :attr:`AuthFileState.ABSENT`):
        the identity file is not itself security-bearing -- it only lets
        ``/auth/login`` check a submitted password against it -- so failing
        closed here would risk bricking a device over a corrupted
        boot-partition file for no benefit. The real adapter logs this at
        ERROR once.

        A transient read failure (:class:`OSError` -- permission denied,
        mount not ready, any other I/O error) says nothing about whether an
        identity file exists at all, so it must not be conflated with clean
        absence. Callers see :data:`IDENTITY_UNAVAILABLE` and must refuse
        both the DIY open-setup and initial-credentials flows rather than
        guessing -- see :func:`~palmimo_portal.core.identity.compute_auth_state`.
        """
        ...

    def read_identity_uncached(self) -> Identity | IdentityUnavailable | None:
        """Return the device identity from a fresh disk read, bypassing any cache.

        Same three-way return type and parsing semantics as
        :meth:`read_identity`, but never serves a cached positive read.
        Callers where a stale cached :class:`Identity` would be dangerous
        (currently only :func:`~palmimo_portal.core.auth.decide_reset`, via
        the unauthenticated ``POST /auth/reset``) must know whether an
        identity file exists on disk *right now*. Implementations should
        still refresh their cache with whatever this call finds, so a
        subsequent :meth:`read_identity` reflects it too.
        """
        ...


@dataclass(frozen=True)
class Release:
    """One GitHub Release, as reported by :class:`ReleaseSource`."""

    tag: str
    name: str
    published_at: str
    html_url: str


@dataclass(frozen=True)
class InstalledVersion:
    """The Portal checkout's currently installed version, as reported by :meth:`Updater.installed`.

    ``tag`` is ``None`` when ``HEAD`` is not exactly on a tag -- not the same
    as "no version can be determined" (``commit`` is only ``None`` when the
    directory is not a git checkout at all).
    """

    tag: str | None
    commit: str | None


class ReleaseSourceError(Exception):
    """Raised by a real :class:`ReleaseSource` when it cannot report the latest release.

    ``code`` is the same snake_case i18n key style as
    :class:`AdapterUnavailableError` -- ``api/update.py`` maps it 1:1 onto a
    ``PortalError`` code.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ReleaseSource(Protocol):
    """Discovers the latest published release, backed by GitHub's Releases API in the real adapter.

    See :class:`~palmimo_portal.adapters.github_releases.GitHubReleaseSource`.
    """

    def fetch_latest(self) -> Release:
        """Return the latest published (non-prerelease, non-draft) release.

        Raises:
            ReleaseSourceError: ``"no_release"`` if the repository has no
                releases at all, or ``"release_source_unavailable"`` for any
                other network/API failure.
        """
        ...


class UpdateStepError(Exception):
    """Raised by :meth:`Updater.apply` when one of its steps fails.

    ``step`` is one of ``"fetch"``, ``"assets"``, ``"checkout"``, ``"sync"``,
    ``"install-assets"`` -- the same step names passed to the ``on_step``
    callback, so a caller can tell which step it failed on without parsing
    ``message``.
    """

    def __init__(self, step: str, message: str) -> None:
        self.step = step
        super().__init__(f"{step}: {message}")


class Updater(Protocol):
    """Reads the installed Portal version and applies an update to it.

    See :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`.
    """

    def installed(self) -> InstalledVersion:
        """Return the Portal checkout's currently installed version."""
        ...

    def apply(self, tag: str, on_step: Callable[[str], None], *, repair_dirty: bool = False) -> None:
        """Check out *tag*, fetch its frontend asset, and sync dependencies, calling ``on_step`` before each step.

        Steps, in order: ``"fetch"``, ``"assets"``, ``"checkout"``,
        ``"sync"``, ``"install-assets"``. See
        :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`'s
        module docstring for the staging order.

        Args:
            repair_dirty: ``True`` when the caller
                (:func:`~palmimo_portal.core.update.should_repair_dirty_checkout`,
                fed from the previous job persisted in ``update.json``) has
                determined a dirty working tree at this point can only be
                debris this same updater left behind at a previous
                ``"fetch"``/``"checkout"`` attempt -- never a USER-made
                change. The real adapter skips its dirty-tree refusal and
                forces the checkout in that case; a USER-dirty tree with
                ``repair_dirty=False`` (the default) still refuses, per
                :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`'s
                module docstring.

        Raises:
            UpdateStepError: a step failed -- ``error.step`` names which one.
        """
        ...


#: The set of states :class:`UpdateJob` can be in over the lifetime of one
#: check/apply/rollback -- see :mod:`palmimo_portal.core.update` for the
#: transition rules between them.
UpdateJobState = Literal["idle", "checking", "running", "restarting", "done", "failed"]

#: Whether an in-flight :class:`UpdateJob` is applying the latest release or
#: rolling back to the previous one -- the same state machine serves both,
#: distinguished only by this field and by which tag :meth:`Updater.apply`
#: was given.
UpdateJobKind = Literal["update", "rollback"]


@dataclass(frozen=True)
class UpdateJob:
    """One in-flight (or just-finished) update/rollback attempt, persisted via :class:`StateStore`."""

    state: UpdateJobState
    kind: UpdateJobKind
    target: str | None
    step: str | None
    error: str | None
    started_at: float | None
    finished_at: float | None
    #: When :func:`~palmimo_portal.core.update.mark_restarting` stamped this
    #: job ``"restarting"`` -- ``None`` until then. Kept separate from
    #: ``started_at`` so :func:`~palmimo_portal.core.update.expire_stale_restart`
    #: can measure its timeout from the restart itself, not the whole apply.
    restarting_at: float | None = None


@dataclass(frozen=True)
class UpdateState:
    """The Portal's whole update picture: the last-checked release, the previous tag, and any job."""

    latest: Release | None
    checked_at: float | None
    previous_tag: str | None
    job: UpdateJob


class StateStore(Protocol):
    """Persists the small pieces of state the Portal must survive a restart.

    Backed by JSON files under ``PALMIMO_STATE_DIR`` in the real adapter
    (:mod:`palmimo_portal.adapters.state`).
    """

    def read_auth(self) -> AuthState | None:
        """Return the stored auth material, or ``None`` if absent or corrupt.

        Callers that must distinguish "no password set yet" from "the file
        is corrupt" -- both of which return ``None`` here -- should check
        :meth:`auth_state` first.
        """
        ...

    def auth_state(self) -> AuthFileState:
        """Classify ``auth.json`` as :attr:`AuthFileState.ABSENT`, :attr:`PRESENT`, or :attr:`CORRUPT`."""
        ...

    def create_auth(self, state: AuthState) -> None:
        """Create the auth material for the first time, atomically.

        Unlike :meth:`write_auth`, this must fail rather than overwrite if
        auth material already exists -- it is the exclusive-create half of
        first-time setup, used so two concurrent ``/setup`` requests cannot
        both "succeed" with the second silently overwriting the first.

        Raises:
            AuthAlreadyExistsError: auth material already exists (including
                a corrupt file -- this call never overwrites either case).
        """
        ...

    def write_auth(self, state: AuthState) -> None:
        """Persist auth material, replacing whatever was stored before.

        For rotating existing auth material (password change, key
        rotation) -- not for first-time creation, which must go through
        :meth:`create_auth` instead so it can enforce exclusivity.
        """
        ...

    def delete_auth(self) -> None:
        """Atomically remove ``auth.json``, returning the device to :attr:`AuthFileState.ABSENT`.

        Backs the unauthenticated login-credentials-reset path
        (``POST /api/v1/auth/reset``, gated to identity-carrying devices
        only -- see :func:`~palmimo_portal.core.auth.decide_reset`): after
        this call, only the manufacturing sticker's initial password can log
        in. Runs inside :meth:`lock_auth`, the same lock
        :func:`~palmimo_portal.core.auth.change_password_from_full` holds,
        so a reset cannot interleave with a password change in flight. A
        no-op, not an error, when ``auth.json`` is already absent; removes
        it just the same when :attr:`AuthFileState.CORRUPT`.

        Deliberately does **not** call :meth:`discard_initial_signing_key`
        -- that key signs *new* initial-mode sessions once the device is
        back in ``auth_state == "initial"``, so discarding it here would
        force a wasteful re-creation on the next login. Implementations
        should instead rotate it in place when one already exists on disk,
        defense in depth against a stale session token surviving the reset.
        """
        ...

    def read_last_wifi_attempt(self) -> WifiAttempt | None:
        """Return the most recent Wi-Fi connect attempt, or ``None`` if none yet."""
        ...

    def write_last_wifi_attempt(self, attempt: WifiAttempt) -> None:
        """Persist the most recent Wi-Fi connect attempt, replacing any prior one."""
        ...

    def read_or_create_initial_signing_key(self) -> str:
        """Return the signing key for initial-mode session tokens, creating it on first use.

        Distinct from :attr:`AuthState.signing_key`, which does not exist
        yet while ``auth.json`` is :attr:`AuthFileState.ABSENT`. Signs
        sessions issued by ``/auth/login`` while ``auth_state == "initial"``
        (identity file present, no owner yet), between the sticker login and
        the forced password change that creates ``auth.json``. Once that
        change succeeds, the new session is re-issued under
        ``AuthState.signing_key`` instead and this key is no longer
        consulted -- see :func:`~palmimo_portal.core.auth.change_password_from_initial`.

        Not a hot path, so implementations re-read from disk on every call
        rather than caching in memory -- a cache could keep serving a key
        that no longer matches what :meth:`discard_initial_signing_key` left
        on disk.
        """
        ...

    def discard_initial_signing_key(self) -> None:
        """Delete the initial-mode session-signing key material, if any.

        Called once :meth:`change_password_from_initial
        <palmimo_portal.core.auth.change_password_from_initial>` has
        successfully created ``auth.json``: leaving the key on disk would
        let a session token minted before promotion keep verifying against
        it if the device ever returns to :attr:`AuthFileState.ABSENT`
        again. A no-op if no such key exists.
        """
        ...

    def lock_auth(self) -> AbstractContextManager[None]:
        """Hold an exclusive lock across a read-verify-write sequence on the auth material.

        Used by :func:`~palmimo_portal.core.auth.change_password_from_full`
        to serialize two concurrent full-mode password changes -- without
        it, the second writer could silently clobber a decision the first
        never saw. Bounded: a contending caller waits up to a fixed timeout
        (:data:`~palmimo_portal.core.auth.AUTH_LOCK_TIMEOUT_SECONDS`) rather
        than forever, so one stuck caller cannot hang every other
        password-change request indefinitely.

        Raises:
            AuthLockTimeoutError: the lock could not be acquired within the
                timeout.
        """
        ...

    def read_update_state(self) -> UpdateState:
        """Return the persisted update state, defaulting to idle when absent or corrupt.

        Unlike ``auth.json``, this is not security-bearing: a missing or
        unparseable ``update.json`` is logged at WARNING and treated as a
        device that has never checked for an update.
        """
        ...

    def write_update_state(self, state: UpdateState) -> None:
        """Persist the update state, replacing whatever was stored before."""
        ...
