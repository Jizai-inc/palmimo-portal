"""Port definitions (:class:`typing.Protocol`) for the Palmimo Portal.

A port is the seam between the use-case layer (``core/``) and the outside
world. ``core/`` depends only on the protocols defined here; the concrete
implementations — real ones that touch the filesystem or D-Bus, and fakes
that hold everything in memory — live in ``adapters/`` and ``testing/``
respectively. Nothing outside this module and ``adapters/`` should need to
know which concrete class backs a port.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractContextManager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Protocol


class ConnectionState(StrEnum):
    """Wi-Fi state machine, mirroring comitup's: no known network yet, mid-attempt, or on the home LAN."""

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
    """The connection/AP name comitup was actually observed on when this attempt resolved.
    ``None`` while still ``"attempting"``, or when the real adapter reported no name. Distinct
    from ``ssid`` (what the client *asked* to connect to) because comitup can settle onto a
    different network than the one just attempted."""


class NetworkPort(Protocol):
    """Wi-Fi state and control. See :class:`~palmimo_portal.adapters.comitup.ComitupNetworkPort`."""

    def get_status(self) -> WifiStatus:
        """Return the current connection state."""
        ...

    def list_networks(self) -> list[WifiNetwork]:
        """Return the most recent nearby-SSID scan."""
        ...

    def has_known_networks(self) -> bool:
        """Report whether any network has ever been configured.

        Used with :meth:`get_status`: a device currently disconnected but with a known
        network on file is provisioned, not out-of-box.
        """
        ...

    def connect(self, ssid: str, psk: str) -> None:
        """Start a connection attempt.

        Returns immediately -- the result is read back later via :class:`StateStore`
        (see :class:`WifiAttempt`), never through this call's return value.
        """
        ...

    def forget_current(self) -> None:
        """Delete the currently connected network's saved profile and drop the connection.

        comitup falls back to HOTSPOT (or another known network, if any). Returns immediately.

        Raises:
            AdapterUnavailableError: the backend cannot be reached.
            NotConnectedError: current state is not CONNECTED, checked with a fresh read
                immediately before deciding -- never a cached value. While in HOTSPOT,
                ``delete_connection()`` deletes the NetworkManager profile of the *active*
                SSID on the link device, i.e. comitup's own hotspot profile, not a
                home-network one.
        """
        ...


class ClockPort(Protocol):
    """Monotonic timekeeping and NTP-sync status. See :class:`~palmimo_portal.adapters.clock.SystemClockPort`.

    ``monotonic()`` is what :mod:`palmimo_portal.core.periodic` schedules
    against -- never wall-clock time, which a device without an RTC can
    jump years on first NTP sync (design doc 3.3).
    """

    def monotonic(self) -> float:
        """Return a monotonic clock reading, in seconds, comparable only to other readings from this port."""
        ...

    def ntp_synchronized(self) -> bool:
        """Report whether the OS clock is NTP-synchronized.

        Every periodic network task (git update checks, the catalog and
        platform-latest refresh) is gated on this -- before sync, TLS to
        GitHub/PyPI fails outright on a device with no RTC (design doc
        3.6). A real adapter that cannot reach ``timedate1`` over D-Bus
        reports ``False`` (fail closed) rather than raising.
        """
        ...


class SystemPort(Protocol):
    """Power operations. See :class:`~palmimo_portal.adapters.systemd.SystemdSystemPort`."""

    def reboot(self) -> None:
        """Reboot the machine."""
        ...

    def shutdown(self) -> None:
        """Shut the machine down safely."""
        ...

    def restart_portal(self) -> None:
        """Restart the Portal's own systemd unit, so freshly ``uv sync``'d code starts running.

        Called by :class:`~palmimo_portal.core.update_runner.UpdateRunner` once
        :class:`Updater.apply` succeeds.

        Raises:
            AdapterUnavailableError: the D-Bus call to systemd timed out or failed.
        """
        ...


class AdapterUnavailableError(Exception):
    """Raised by a real :class:`NetworkPort`/:class:`SystemPort` when its OS backend cannot be reached.

    Covers a D-Bus call that times out or fails even after the adapter's own reconnect-and-retry
    (see :mod:`palmimo_portal.adapters.dbus_support`). Distinct from a bare :class:`Exception` so
    ``api/`` can translate it into a 503 ``*_backend_unavailable`` envelope instead of a generic
    500; ``code`` is the same snake_case i18n key the envelope carries (e.g.
    ``"network_backend_unavailable"``), so ``api/`` need not hardcode adapter-to-code mapping.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class NotConnectedError(Exception):
    """Raised by :meth:`NetworkPort.forget_current` when the device is not currently CONNECTED.

    ``api/wifi.py``'s ``DELETE /wifi/connection`` translates this to 409 ``wifi_not_connected``,
    rather than letting the adapter delete the HOTSPOT's own profile (see :meth:`NetworkPort.forget_current`).
    Both the real adapter and :class:`~palmimo_portal.testing.fakes.FakeNetworkPort` check a
    freshly read state immediately before deciding whether to raise this -- never a cached value.
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
    """Raised when the ``authorized_keys`` lock cannot be acquired in time.

    Mirrors :class:`AuthLockTimeoutError`: bounded, so one stuck contender cannot hang every
    other key-management request indefinitely -- ``api/`` translates this into 409 ``ssh_keys_busy``.
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
        """Remove the key with the given fingerprint. ``allow_last`` must be ``True`` to remove the last one.

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

    Present only on a device Jizai provisioned before shipping: the sticker's ``device_id`` and
    the sticker's random password, in plaintext (spec v2 -- the boot partition already holds it
    physically, so hashing bought no confidentiality, only a hash/print mismatch risk). Absent on
    a DIY, self-flashed image, which falls back to the legacy open first-time-setup flow (see
    :class:`~palmimo_portal.core.identity.PortalAuthState`).
    """

    device_id: str
    initial_password: str = field(repr=False)


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
    """Reads the manufacturing-written identity file (``PALMIMO_IDENTITY_FILE``). The Portal never writes it."""

    def read_identity(self) -> Identity | IdentityUnavailable | None:
        """Return the device identity, ``None`` for clean absence, or :data:`IDENTITY_UNAVAILABLE`.

        A malformed (but present) file is treated the same as absent (unlike ``auth.json``, which
        distinguishes :attr:`AuthFileState.CORRUPT` from :attr:`AuthFileState.ABSENT`): the
        identity file is not itself security-bearing, so failing closed here would risk bricking a
        device over a corrupted boot-partition file for no benefit. The real adapter logs this at
        ERROR once.

        A transient read failure (:class:`OSError`) says nothing about whether an identity file
        exists at all, so it must not be conflated with clean absence -- callers see
        :data:`IDENTITY_UNAVAILABLE` and must refuse both the DIY open-setup and
        initial-credentials flows rather than guessing.
        """
        ...

    def read_identity_uncached(self) -> Identity | IdentityUnavailable | None:
        """Same as :meth:`read_identity` but bypasses any cache, for callers where a stale cached
        :class:`Identity` would be dangerous (currently only the unauthenticated
        ``POST /auth/reset``). Implementations should still refresh their cache with what this finds.
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

    ``code`` is the same snake_case i18n key style as :class:`AdapterUnavailableError` --
    ``api/update.py`` maps it 1:1 onto a ``PortalError`` code.
    """

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(f"{code}: {message}")


class ReleaseSource(Protocol):
    """Discovers the latest published release. See :class:`~palmimo_portal.adapters.github_releases.GitHubReleaseSource`."""

    def fetch_latest(self) -> Release:
        """Return the latest published (non-prerelease, non-draft) release.

        Raises:
            ReleaseSourceError: ``"no_release"`` if the repository has no
                releases at all, or ``"release_source_unavailable"`` for any
                other network/API failure.
        """
        ...


@dataclass(frozen=True)
class CatalogEnvSpec:
    """One catalog app's declared ``[env.*]`` entry -- the subset the release workflow publishes (design doc 4.1)."""

    required: bool
    description: str
    #: ``None`` if absent or not a plain ``http(s)://`` URL -- see
    #: :func:`~palmimo_portal.adapters.catalog._parse_env`.
    help_url: str | None = None


@dataclass(frozen=True)
class CatalogApp:
    """One official app, as listed in the devkit release's catalog asset (design doc 4.1)."""

    name: str
    description: str
    source: AppSource
    env: dict[str, CatalogEnvSpec]
    devices: tuple[str, ...]


@dataclass(frozen=True)
class CatalogAsset:
    """One fetched, verified, and schema-validated catalog release."""

    tag: str
    apps: tuple[CatalogApp, ...]


class CatalogSourceError(Exception):
    """Raised by :meth:`CatalogSource.fetch` when the release, asset, or its shape is unusable.

    ``code``, when set, is a :class:`ReleaseSourceError`'s own ``code``
    propagated through -- currently only ``"rate_limited"``, which
    :class:`~palmimo_portal.core.catalog.CatalogCache` maps onto its own
    ``reason`` instead of the generic ``"offline"``.
    """

    def __init__(self, message: str, *, code: str | None = None) -> None:
        self.code = code
        super().__init__(message)


class CatalogSource(Protocol):
    """Fetches and validates the official app catalog. See :class:`~palmimo_portal.adapters.catalog.GitHubCatalogSource`.

    One call does the whole pipeline (find the latest devkit release,
    download the catalog asset and its ``.sha256``, verify, parse, and
    validate) -- unlike :class:`PlatformBundleSource`, there is no
    multi-step job to report progress for.
    """

    def fetch(self) -> CatalogAsset:
        """Return the latest official catalog.

        Raises:
            CatalogSourceError: the release/asset could not be fetched, the
                checksum did not match, or the asset's shape is invalid
                (wrong ``schema``, an app missing a required field).
        """
        ...


@dataclass(frozen=True)
class CatalogCacheState:
    """The last-known-good catalog, persisted so a restart does not lose it (design doc 4.1/3.6).

    ``fetched_at`` is a wall-clock ``time.time()`` reading -- display only,
    never compared against for interval/staleness logic (design doc 3.3);
    :class:`~palmimo_portal.core.catalog.CatalogCache` tracks its own
    monotonic age separately, in memory only.
    """

    tag: str | None
    apps: tuple[CatalogApp, ...]
    fetched_at: float | None


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
    """Reads the installed Portal version and applies an update. See :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`."""

    def installed(self) -> InstalledVersion:
        """Return the Portal checkout's currently installed version."""
        ...

    def apply(self, tag: str, on_step: Callable[[str], None]) -> None:
        """Check out *tag*, fetch its frontend asset, and sync dependencies, calling ``on_step`` before each step.

        Steps, in order: ``"fetch"``, ``"assets"``, ``"checkout"``, ``"sync"``, ``"install-assets"``
        -- see :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`'s module docstring.

        Raises:
            UpdateStepError: a step failed -- ``error.step`` names which one.
        """
        ...


#: States :class:`UpdateJob` can be in over one check/apply/rollback -- see
#: :mod:`palmimo_portal.core.update` for the transition rules.
UpdateJobState = Literal["idle", "checking", "running", "restarting", "done", "failed"]

#: Whether an in-flight :class:`UpdateJob` is applying the latest release or rolling back --
#: the same state machine serves both, distinguished by this field and by the tag passed to
#: :meth:`Updater.apply`.
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

    Backed by JSON files under ``PALMIMO_STATE_DIR`` (:mod:`palmimo_portal.adapters.state`).
    """

    def read_auth(self) -> AuthState | None:
        """Return the stored auth material, or ``None`` if absent or corrupt.

        Callers that must distinguish "no password set yet" from "the file is corrupt" --
        both of which return ``None`` here -- should check :meth:`auth_state` first.
        """
        ...

    def auth_state(self) -> AuthFileState:
        """Classify ``auth.json`` as :attr:`AuthFileState.ABSENT`, :attr:`PRESENT`, or :attr:`CORRUPT`."""
        ...

    def create_auth(self, state: AuthState) -> None:
        """Create the auth material for the first time, atomically.

        Unlike :meth:`write_auth`, must fail rather than overwrite if auth material already
        exists -- the exclusive-create half of first-time setup, so two concurrent ``/setup``
        requests cannot both "succeed" with the second silently overwriting the first.

        Raises:
            AuthAlreadyExistsError: auth material already exists (including a corrupt file --
                this call never overwrites either case).
        """
        ...

    def write_auth(self, state: AuthState) -> None:
        """Persist auth material, replacing whatever was stored before.

        For rotating existing material (password change, key rotation) -- not for first-time
        creation, which must go through :meth:`create_auth` to enforce exclusivity.
        """
        ...

    def delete_auth(self) -> None:
        """Atomically remove ``auth.json``, returning the device to :attr:`AuthFileState.ABSENT`.

        Backs the unauthenticated login-credentials-reset path (``POST /api/v1/auth/reset``,
        gated to identity-carrying devices only -- see
        :func:`~palmimo_portal.core.auth.decide_reset`): after this call, only the
        manufacturing sticker's initial password can log in. Runs inside :meth:`lock_auth`, the
        same lock :func:`~palmimo_portal.core.auth.change_password_from_full` holds, so a reset
        cannot interleave with a password change in flight. A no-op, not an error, when
        ``auth.json`` is already absent; removes it just the same when
        :attr:`AuthFileState.CORRUPT`.

        Deliberately does **not** call :meth:`discard_initial_signing_key` -- that key signs
        *new* initial-mode sessions, so discarding it here would force a wasteful re-creation on
        the next login. Implementations should instead rotate it in place when one already exists
        on disk, defense in depth against a stale session token surviving the reset.
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

        Distinct from :attr:`AuthState.signing_key`, which doesn't exist yet while ``auth.json``
        is :attr:`AuthFileState.ABSENT`. Signs sessions issued by ``/auth/login`` while
        ``auth_state == "initial"``, between the sticker login and the forced password change
        that creates ``auth.json``; once that succeeds this key is no longer consulted -- see
        :func:`~palmimo_portal.core.auth.change_password_from_initial`.

        Not a hot path, so implementations re-read from disk on every call rather than caching --
        a cache could keep serving a key that no longer matches what
        :meth:`discard_initial_signing_key` left on disk.
        """
        ...

    def discard_initial_signing_key(self) -> None:
        """Delete the initial-mode session-signing key material, if any. A no-op if none exists.

        Called once :meth:`change_password_from_initial
        <palmimo_portal.core.auth.change_password_from_initial>` has created ``auth.json``:
        leaving the key on disk would let a session token minted before promotion keep verifying
        against it if the device ever returns to :attr:`AuthFileState.ABSENT`.
        """
        ...

    def lock_auth(self) -> AbstractContextManager[None]:
        """Hold an exclusive lock across a read-verify-write sequence on the auth material.

        Used by :func:`~palmimo_portal.core.auth.change_password_from_full` to serialize two
        concurrent full-mode password changes -- without it, the second writer could silently
        clobber a decision the first never saw. Bounded
        (:data:`~palmimo_portal.core.auth.AUTH_LOCK_TIMEOUT_SECONDS`), so one stuck caller
        cannot hang every other password-change request indefinitely.

        Raises:
            AuthLockTimeoutError: the lock could not be acquired within the timeout.
        """
        ...

    def read_update_state(self) -> UpdateState:
        """Return the persisted update state, defaulting to idle when absent or corrupt.

        Unlike ``auth.json``, not security-bearing: a missing or unparseable ``update.json`` is
        logged at WARNING and treated as a device that has never checked for an update.
        """
        ...

    def write_update_state(self, state: UpdateState) -> None:
        """Persist the update state, replacing whatever was stored before."""
        ...

    def read_apps_state(self) -> AppsState:
        """Return the persisted app ledger, defaulting to empty when absent.

        Unlike ``update.json``, a corrupt (present but unparseable) file is
        never conflated with "no apps installed" here -- see
        :meth:`apps_state_file_state`. Callers that skip that check first
        and read a corrupt ledger as empty would make every installed app
        vanish from the UI and stop autostart from ever firing.
        """
        ...

    def apps_state_file_state(self) -> AppsStateFileState:
        """Classify ``apps.json`` as :attr:`AppsStateFileState.ABSENT`, :attr:`PRESENT`, or :attr:`CORRUPT`.

        ``api/apps.py`` checks this before every apps-tab request; on
        :attr:`~AppsStateFileState.CORRUPT` it answers 409
        ``platform_state_corrupt`` for the whole apps tab rather than
        guessing. Recovery is ``POST /apps/reset``.
        """
        ...

    def write_apps_state(self, state: AppsState) -> None:
        """Persist the app ledger, replacing whatever was stored before."""
        ...

    def lock_apps(self) -> AbstractContextManager[None]:
        """Hold the exclusive lock serializing install/update/delete jobs (``apps.lock``).

        Exactly one app job runs at a time (see the technical design's
        "does not run concurrently" invariant): a second job attempted
        while one is in flight must see a bounded, non-blocking failure,
        not queue silently behind the first.

        Raises:
            AppsLockTimeoutError: the lock could not be acquired within
                :data:`~palmimo_portal.core.apps_jobs.APPS_LOCK_TIMEOUT_SECONDS`.
        """
        ...

    def lock_run(self) -> AbstractContextManager[None]:
        """Hold the exclusive, non-blocking lock serializing start/stop/autostart (``run.lock``).

        Independent of :meth:`lock_apps` (design doc 2.1): an app can be
        started while another is mid-install/update, but two starts (or a
        start racing autostart) must never both observe "nothing running"
        and both proceed.

        Raises:
            RunLockTimeoutError: another start/stop/autostart already holds it.
        """
        ...

    def read_platform_update_state(self) -> PlatformUpdateState:
        """Return the persisted platform-update job, defaulting to idle when absent or corrupt.

        Not security-bearing, same tolerant-read contract as
        :meth:`read_update_state`: a missing or unparseable
        ``platform_update.json`` is logged at WARNING and treated as "no
        platform job has ever run".
        """
        ...

    def write_platform_update_state(self, state: PlatformUpdateState) -> None:
        """Persist the platform-update job, replacing whatever was stored before."""
        ...

    def lock_platform(self) -> AbstractContextManager[None]:
        """Hold the exclusive, non-blocking lock serializing platform-bundle update jobs.

        Mirrors :meth:`lock_run`'s non-blocking shape: a second ``POST
        /platform/update`` while one is already running must see an
        immediate, bounded failure, not queue behind it.

        Raises:
            PlatformLockTimeoutError: another platform update already holds it.
        """
        ...

    def read_catalog_cache(self) -> CatalogCacheState:
        """Return the last successfully fetched official catalog, or an empty state if none yet.

        Tolerant read, same contract as :meth:`read_platform_update_state`:
        a missing or unparseable ``catalog_cache.json`` is logged and
        treated as "never fetched" rather than raised -- this cache is a
        staleness fallback, not security- or ledger-bearing.
        """
        ...

    def write_catalog_cache(self, state: CatalogCacheState) -> None:
        """Persist the last successfully fetched official catalog, replacing whatever was stored before."""
        ...


class AppsLockTimeoutError(Exception):
    """Raised by :meth:`StateStore.lock_apps` when another app job already holds the lock.

    ``api/apps.py`` translates this into 409 ``app_job_in_progress``.
    """


class AppsStateFileState(StrEnum):
    """The three states ``apps.json`` can be in, mirroring :class:`AuthFileState`.

    A corrupt ledger must never be treated as "no apps installed" -- doing
    so would make every installed app silently disappear from the UI and
    stop autostart from ever running. Recovery is manual: ``POST /apps/reset``.
    """

    ABSENT = "absent"
    PRESENT = "present"
    CORRUPT = "corrupt"


AppSourceType = Literal["zip", "git"]
AppRefKind = Literal["branch", "tag"]
AppJobKind = Literal["install", "update", "delete"]
AppJobState = Literal["running", "done", "failed"]


@dataclass(frozen=True)
class AppSource:
    """Where an app's files came from -- persisted so ``update``/``preview`` know how to refetch it."""

    type: AppSourceType
    url: str | None = None
    ref: str | None = None
    ref_kind: AppRefKind | None = None
    subdir: str | None = None
    commit: str | None = None
    #: The manifest file this app was installed from, when it is not the default
    #: ``palmimo.toml`` -- ``None`` means the default. Fixed at install time; ``update`` re-reads
    #: this same file.
    manifest: str | None = None


@dataclass(frozen=True)
class AppJob:
    """One install/update/delete attempt against a single app, persisted on :class:`AppRecord`."""

    id: str
    kind: AppJobKind
    state: AppJobState
    step: str | None
    error: str | None
    started_at: float | None
    finished_at: float | None
    #: True when no ``uv.lock`` was found in the app's project and one was generated --
    #: surfaced so the UI can warn "reproducibility reduced" without failing the job.
    lock_generated: bool = False
    #: Env var request names whose binding no longer names a declared ``[env.*]`` entry
    #: after this job (manifest changed) -- see design doc 3.4's "register" step.
    dropped_bindings: tuple[str, ...] = ()
    dropped_params: tuple[str, ...] = ()


@dataclass(frozen=True)
class AppRecord:
    """One entry in the app ledger (``apps.json``)."""

    name: str
    source: AppSource
    installed_at: float | None
    params: dict[str, Any]
    autostart: bool
    last_job: AppJob | None
    #: Set by startup finalize when the manifest or ``.venv`` this record
    #: points at is missing on disk -- see design doc 3.3's finalize rule.
    broken_reason: str | None = None
    #: Set when a git update/check got HTTP 401/403 from this app's credential --
    #: further periodic checks stop until the credential is replaced (design doc 3.6).
    credential_rejected: bool = False
    #: Set by the periodic git-check sweep (:mod:`palmimo_portal.core.periodic`) for a
    #: branch-pinned app with a newer upstream commit than `source.commit`.
    update_available: bool = False
    #: The upstream commit `update_available` refers to, or `None` before any check has run.
    latest_commit: str | None = None


@dataclass(frozen=True)
class AppsState:
    """The whole app ledger: every installed app, plus any job currently in flight.

    ``current_job`` names the one app-job the ``apps.lock`` is guarding, if any --
    distinct from each :class:`AppRecord`'s own ``last_job``, which is that app's
    most recently *finished* (or interrupted) job, kept after ``current_job`` clears.
    """

    apps: dict[str, AppRecord] = field(default_factory=dict)
    current_job: AppJob | None = None
    current_job_app: str | None = None


class AppExistsError(Exception):
    """Raised when installing over a name already in the ledger. ``api/apps.py`` maps this to 409 ``app_exists``."""


class AppNotFoundError(Exception):
    """Raised when an operation names an app absent from the ledger."""


class AppJobInProgressError(Exception):
    """Raised when an app job is attempted while another (any app) is already running.

    Distinct from :class:`~palmimo_portal.ports.AppsLockTimeoutError`: this
    is the use-case-level rule (:mod:`palmimo_portal.core.apps_jobs`), that
    exception is the lock primitive itself -- both map to the same 409
    ``app_job_in_progress``.
    """


class PortalUpdateInProgressError(Exception):
    """Raised when starting an app job while a Portal self-update is running.

    ``api/apps.py`` maps this to 409 ``update_in_progress`` -- the same
    code an app job's own in-progress state maps onto for a Portal update
    attempt (see ``api/update.py``), so the two features refuse each other
    symmetrically.
    """


class DiskFullError(Exception):
    """Raised by a job's disk-space precheck. ``api/apps.py`` maps this to 507 ``disk_full``."""


class AppsDirUnavailableError(Exception):
    """Raised by a job's disk-space precheck when ``apps_dir`` itself cannot be statted.

    Distinct from :class:`DiskFullError`: ``apps_dir`` not existing means the
    platform bundle's ``StateDirectory`` was never provisioned (or is not
    yet mounted), not that the volume is full -- ``api/apps.py`` maps this
    to 503 ``platform_not_ready`` rather than 507 ``disk_full``.
    """


class InvalidManifestSourceError(Exception):
    """Raised when a job's fetched source fails validation (bad zip, no manifest, wrong app name, ...)."""


@dataclass(frozen=True)
class SecretRecord:
    """One registered secret's metadata -- never its value. See :class:`SecretsStore`."""

    name: str
    updated_at: float


@dataclass(frozen=True)
class GitCredentialRecord:
    """One registered GitHub host/owner credential's metadata -- never its token."""

    host_owner: str
    updated_at: float
    #: Set by :meth:`SecretsStore.mark_git_credential_rejected` when a git operation using this
    #: credential got HTTP 401/403 -- cleared back to `None` by :meth:`SecretsStore.set_git_credential`,
    #: since a rewritten credential must resume periodic checks (design doc 3.6/4.2).
    rejected_at: float | None = None
    rejected_status: int | None = None


class InvalidSecretNameError(Exception):
    """Raised when a secret/store name does not match ``^[A-Z][A-Z0-9_]{0,63}$``."""


class InvalidSecretValueError(Exception):
    """Raised when a secret value contains a newline -- design doc 3.5's env-file line format forbids it."""


class SecretNotFoundError(Exception):
    """Raised by :meth:`SecretsStore.delete_secret` when no secret has the given name."""


class SecretInUseError(Exception):
    """Raised by :meth:`SecretsStore.delete_secret` when a binding still references it.

    ``users`` lists ``(app_name, request_name)`` pairs still bound to the secret being
    deleted -- ``api/secrets.py`` returns them in the 409 ``secret_in_use`` body.
    """

    def __init__(self, users: list[tuple[str, str]]) -> None:
        self.users = users
        super().__init__(f"secret in use by: {users}")


class UnknownSecretNameError(Exception):
    """Raised by :meth:`SecretsStore.write_bindings` when a binding names an unregistered secret."""


class InvalidHostOwnerError(Exception):
    """Raised when a git-credential ``host_owner`` scope does not normalize to ``host/owner`` shape.

    See :func:`~palmimo_portal.core.secrets.normalize_host_owner` for the
    normalization/validation rule.
    """


class SecretsStore(Protocol):
    """Persists registered secret values, per-app bindings, and git credentials.

    Backed by ``secrets/{values,bindings,git_credentials}.json`` under
    ``PALMIMO_SECRETS_DIR`` (0700; each file 0600) --
    :mod:`palmimo_portal.adapters.secrets`. Values are write-only from the
    API's point of view: nothing in ``api/`` ever reads
    :meth:`get_secret_value` back out to a client.
    """

    def list_secrets(self) -> list[SecretRecord]:
        """Return every registered secret's name and last-update time, never its value."""
        ...

    def set_secret(self, name: str, value: str) -> None:
        """Register or overwrite a secret value.

        Raises:
            InvalidSecretNameError: ``name`` does not match ``^[A-Z][A-Z0-9_]{0,63}$``.
            InvalidSecretValueError: ``value`` contains a newline.
        """
        ...

    def get_secret_value(self, name: str) -> str | None:
        """Return the raw value for ``name``, or ``None`` if unregistered. Never exposed via the API."""
        ...

    def delete_secret(self, name: str) -> None:
        """Remove a registered secret.

        Raises:
            SecretNotFoundError: no secret is registered under ``name``.
            SecretInUseError: some app's binding still references it.
        """
        ...

    def read_bindings(self, app: str) -> dict[str, str]:
        """Return ``app``'s ``{request_name: registered_secret_name}`` map, or ``{}`` if none set."""
        ...

    def write_bindings(self, app: str, bindings: dict[str, str]) -> None:
        """Replace ``app``'s bindings wholesale.

        Raises:
            UnknownSecretNameError: some value in ``bindings`` names a secret that is not registered.
        """
        ...

    def delete_bindings(self, app: str) -> None:
        """Remove every binding for ``app`` (called when the app itself is deleted). A no-op if none exist."""
        ...

    def bindings_using(self, secret_name: str) -> list[tuple[str, str]]:
        """Return every ``(app, request_name)`` pair currently bound to ``secret_name``."""
        ...

    def list_git_credentials(self) -> list[GitCredentialRecord]:
        """Return every registered ``host/owner`` credential's metadata, never its token."""
        ...

    def set_git_credential(self, host_owner: str, token: str) -> None:
        """Register or overwrite the token for ``host_owner`` (e.g. ``github.com/Jizai-inc``)."""
        ...

    def get_git_credential(self, host_owner: str) -> str | None:
        """Return the raw token for ``host_owner``, or ``None`` if unregistered. Never exposed via the API."""
        ...

    def delete_git_credential(self, host_owner: str) -> None:
        """Remove a registered git credential. A no-op if none exists."""
        ...

    def mark_git_credential_rejected(self, host_owner: str, status: int) -> None:
        """Record that a git operation using ``host_owner``'s credential got HTTP ``status`` (401/403).

        A no-op if ``host_owner`` is not registered. Called by
        :func:`~palmimo_portal.core.periodic.run_git_check_sweep` in place
        of the periodic check itself, so :meth:`list_git_credentials`
        reflects the same rejection ``GET /git-credentials`` reports.
        """
        ...

    def reset(self) -> None:
        """Erase every secret value, binding, and git credential (``POST /apps/reset``, design doc 3.2)."""
        ...


class GitCommandError(Exception):
    """Raised by a real :class:`GitPort` when a git subprocess fails, times out, or cannot start.

    ``status_code`` is the HTTP status git's own stderr reported (401/403
    for a rejected credential), when the real adapter could parse one out
    -- see :mod:`palmimo_portal.core.periodic`'s credential-rejection
    handling. ``None`` for every other failure (timeout, unknown ref, no
    HTTP status in the output).
    """

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        self.status_code = status_code
        super().__init__(message)


class GitPort(Protocol):
    """Runs the ``git`` subprocesses an app install/update job needs. See :class:`~palmimo_portal.adapters.git_port.SubprocessGitPort`.

    Every method is non-interactive and timeout-bounded
    (``GIT_TERMINAL_PROMPT=0``, ``GIT_ASKPASS=/bin/true``): a stale or
    revoked credential must fail fast, never hang the job waiting on a
    prompt no one can answer.
    """

    def clone_shallow(
        self, url: str, ref: str, ref_kind: AppRefKind, dest: Path, *, env: Mapping[str, str] | None = None
    ) -> str:
        """Shallow-clone ``url`` at ``ref`` into ``dest`` (created fresh). Returns the resulting commit SHA.

        ``env`` carries git-credential ``GIT_CONFIG_*`` variables (design
        doc 4.2) -- never passed as argv, which ``/proc/<pid>/cmdline``
        exposes to every UID on the device.

        Raises:
            GitCommandError: the clone failed, timed out, or the ref does not exist.
        """
        ...

    def fetch_commit(self, url: str, ref: str, ref_kind: AppRefKind, *, env: Mapping[str, str] | None = None) -> str:
        """Resolve ``ref`` on ``url`` to a commit SHA without cloning -- used by ``update/check``.

        Raises:
            GitCommandError: the fetch failed, timed out, or the ref does not exist.
        """
        ...


class UvPort(Protocol):
    """Reports the ``uv`` binary's version. See :class:`~palmimo_portal.adapters.uv_port.SubprocessUvPort`.

    Dependency sync no longer runs in the Portal's own process (design doc
    2.2b): an untrusted app's ``pyproject.toml`` can run arbitrary code via
    its build backend or path dependencies, so sync runs as ``palmimo-app``
    through :class:`SyncUnitPort` instead. This port is left with only the
    one call that never touches app code.
    """

    def version(self) -> str:
        """Return ``uv --version``'s output, or ``"unknown"`` if it could not be determined.

        Never raises -- consumed only by ``GET /apps/{name}/diagnostics``
        (design doc 3.2), where a version string a support agent cannot get
        is more useful than a 500.
        """
        ...


class SyncFailedError(Exception):
    """Raised by :mod:`palmimo_portal.core.apps_jobs` when a :class:`SyncUnitPort` job unit did not succeed.

    Carries systemd's own ``Result``/``ExecMainStatus`` pair plus the
    unit's masked journal tail (design doc 2.2b) -- ``str(error)`` is what
    ends up in the job's persisted ``error`` field, shown to the operator.
    """

    def __init__(self, instance: str, result: str, exec_main_status: int, journal_tail: str) -> None:
        self.result = result
        self.exec_main_status = exec_main_status
        message = f"dependency sync for {instance} failed: result={result} exec_main_status={exec_main_status}"
        if journal_tail:
            message = f"{message}\n{journal_tail}"
        super().__init__(message)


class SyncUnitPort(Protocol):
    """Controls one app job's ``palmimo-app-sync@<instance>.service`` oneshot unit (design doc 2.2b).

    ``instance`` is the ``.staging/<id>/`` directory name the job is
    running against -- also the value the caller wrote into that
    directory's ``sync.json`` before calling :meth:`start`. Runs as
    ``palmimo-app``, the same uid as the app itself, never the Portal's own
    -- an app author's ``pyproject.toml`` build backend/path dependencies
    must not run with the Portal's privileges.
    """

    def start(self, instance: str) -> None:
        """Ask systemd to start the sync unit for ``instance``. Returns once queued, not once finished."""
        ...

    def wait(self, instance: str, timeout_s: float) -> UnitStatus:
        """Poll until the unit leaves ``active``/``activating``, returning its final status.

        Raises:
            TimeoutError: the unit was still running after ``timeout_s``.
        """
        ...

    def stop(self, instance: str) -> None:
        """Ask systemd to stop the sync unit for ``instance`` and wait for the stop job to finish."""
        ...

    def list_active_instances(self) -> list[str]:
        """Return the instance names of every ``palmimo-app-sync@*`` unit that is active/activating/deactivating.

        Used only at startup (design doc 2.2b): a process that died mid-job
        can leave its sync unit running against a ``.staging/<id>/`` tree
        :func:`~palmimo_portal.core.apps_jobs.cleanup_staging_and_trash` is
        about to purge -- that unit must be stopped first.
        """
        ...


class DiskPort(Protocol):
    """Reports free disk space. See :class:`~palmimo_portal.adapters.disk.OsDiskPort`."""

    def free_bytes(self, path: Path) -> int:
        """Return the free space, in bytes, on the filesystem containing ``path``."""
        ...


class RunLockTimeoutError(Exception):
    """Raised by :meth:`StateStore.lock_run` when start/stop/autostart's ``run.lock`` is already held.

    ``api/apps.py`` translates this into 409 ``app_busy``. Distinct from
    :class:`AppsLockTimeoutError`: install/update/delete and start/stop are
    independent axes (design doc 2.1) -- an app can be started while
    another is mid-update, but two starts (or a start racing autostart)
    must never both observe "nothing running" and both proceed.
    """


@dataclass(frozen=True)
class UnitStatus:
    """systemd's view of one ``palmimo-app@<name>.service`` unit, as read by :meth:`AppUnitPort.status`.

    ``result`` and ``exec_main_status`` are ``systemctl show``'s ``Result``/``ExecMainStatus``
    properties, used as-is (design doc 3.5) rather than re-encoded -- ``core.apps_start``
    derives the user-facing status from this pair.
    """

    active_state: str
    sub_state: str
    result: str
    exec_main_status: int
    exec_main_start_timestamp: float = 0.0


class PolkitDeniedError(Exception):
    """Raised by a real :class:`AppUnitPort` when systemd's D-Bus call is refused by polkit.

    Distinct from :class:`AdapterUnavailableError` (design doc 3.5): a
    stale polkit rule on the device, not a transient D-Bus outage --
    ``api/apps.py`` maps this to a dedicated ``polkit_denied`` code instead
    of retrying it like a generic backend hiccup.
    """

    def __init__(self, unit: str, verb: str) -> None:
        self.unit = unit
        self.verb = verb
        super().__init__(f"polkit denied unit={unit} verb={verb}")


class AppUnitPort(Protocol):
    """Controls one app's ``palmimo-app@<name>.service`` unit over systemd's D-Bus API.

    See :class:`~palmimo_portal.adapters.systemd.SystemdAppUnitPort`; every
    method takes the bare app ``name``, not the unit string, and raises
    :class:`PolkitDeniedError` rather than :class:`AdapterUnavailableError`
    when systemd itself refuses the verb (design doc 2.6).
    """

    def set_device_allow(self, name: str, specs: list[str]) -> None:
        """Reset, then set, ``DeviceAllow`` for ``name``'s runtime scope (design doc 2.4).

        Always clears to the empty list first so a previous start's grants
        never linger into this one, then applies ``specs`` (each a
        ``"<class> rw"``-shaped systemd device-allow spec).
        """
        ...

    def start(self, name: str) -> None:
        """Ask systemd to start ``name``'s unit. Returns once queued (``activating``), not once running."""
        ...

    def stop(self, name: str) -> None:
        """Ask systemd to stop ``name``'s unit and wait for the stop job to finish."""
        ...

    def status(self, name: str) -> UnitStatus:
        """Read ``name``'s current ``ActiveState``/``SubState``/``Result``/``ExecMainStatus``."""
        ...

    def list_active_app_units(self) -> list[str]:
        """Return the app names (not unit strings) of every ``palmimo-app@*`` unit that is active/activating/deactivating."""
        ...


class RunDirPort(Protocol):
    """Writes/removes ``/run/palmimo/apps/<name>/`` for one app's next start (design doc 2.1/3.5 step 6).

    See :class:`~palmimo_portal.adapters.rundir.TmpfsRunDirPort`.
    """

    def write(self, name: str, *, env: dict[str, str], argv: list[str], cwd: str, project: str) -> None:
        """Recreate ``name``'s run directory (removing any leftover first) and write ``env``/``argv.json`` into it.

        ``env`` becomes ``KEY=value`` lines (0600, root-readable only --
        PID1 reads it as ``EnvironmentFile=``); ``argv.json`` is
        ``{"argv": argv, "cwd": cwd, "project": project}`` (0640, read by
        ``app-launch`` running as the app's own uid). Group ownership is
        set to ``palmimo-apps`` when that group resolves on this host,
        skipped silently otherwise (a dev machine with no such group).
        """
        ...

    def remove(self, name: str) -> None:
        """Remove ``name``'s run directory. A no-op if it does not exist."""
        ...

    def remove_all(self) -> None:
        """Remove every app's run directory (``POST /apps/reset``, design doc 3.2). A no-op if none exist."""
        ...


@dataclass(frozen=True)
class JournalEntry:
    """One journal line read by :meth:`JournalPort.read`."""

    message: str
    timestamp: float | None
    invocation_id: str | None


@dataclass(frozen=True)
class JournalPage:
    """One page of journal entries, plus a cursor for the next page and every invocation id seen so far."""

    entries: list[JournalEntry]
    next_cursor: str | None
    invocations: list[str]


class JournalPort(Protocol):
    """Reads a unit's journal. See :class:`~palmimo_portal.adapters.journal.JournalctlPort`."""

    def read(self, unit: str, *, cursor: str | None, lines: int, invocation: str | None = None) -> JournalPage:
        """Return up to ``lines`` journal entries for ``unit``, starting after ``cursor`` if given.

        ``invocation``, when given, restricts to that one systemd
        invocation (design doc 3.2's "pick a previous start" UI) --
        :attr:`JournalPage.invocations` lists every id available to pick from.
        """
        ...

    def can_read(self) -> bool:
        """Report whether this process can read the journal at all.

        Backed by ``systemd-journal`` group membership (``os.getgroups()``)
        -- a Portal deployed without that supplementary group gets a
        precheck failure instead of a confusing empty log (design doc 3.2).
        """
        ...


@dataclass(frozen=True)
class PlatformInstalled:
    """The realtime platform bundle currently recorded on this device (``installed.json``, design doc 2.8)."""

    version: int
    installed_at: str
    bundle_sha256: str


@dataclass(frozen=True)
class PlatformManifest:
    """One platform bundle release's ``manifest.json`` (design doc 2.8)."""

    version: int
    requires_portal: str
    restart_portal: bool
    summary: str
    reflash_required: bool


@dataclass(frozen=True)
class PlatformVerifyDiff:
    """One line of ``install.sh verify``'s JSON output -- a single owned path that does not match the manifest.

    ``expected``/``actual`` are set only for a ``"mode"``/``"owner"``/``"group"``
    diff (see ``verify_platform.py``'s ``_diff``); ``None`` for a shape like
    ``"missing"``/``"content"`` that carries no such pair.
    """

    path: str
    kind: str
    expected: str | None = None
    actual: str | None = None


class PlatformCommandError(Exception):
    """Raised by a real :class:`PlatformPort` when ``install.sh`` exits non-zero, times out, or cannot start.

    ``step`` is one of ``"verify"``/``"install"``/``"record"`` -- the
    :class:`PlatformPort` method that failed, mirroring
    :class:`UpdateStepError`'s ``step`` attribute.
    """

    def __init__(self, step: str, message: str) -> None:
        self.step = step
        super().__init__(f"{step}: {message}")


class PlatformPort(Protocol):
    """Reads/applies the realtime platform bundle (design doc 2.8). See :class:`~palmimo_portal.adapters.platform.SudoPlatformPort`.

    The only port whose real implementation runs a command through
    ``sudo`` -- :attr:`~palmimo_portal.settings.Settings.sudo_bin` is
    referenced nowhere else in this tree (``tests/test_platform_sudo_contract.py``).
    """

    def read_installed(self) -> PlatformInstalled | None:
        """Return the currently-recorded platform bundle, or ``None`` if ``installed.json`` is absent/unreadable."""
        ...

    def verify(self, bundle_dir: Path) -> list[PlatformVerifyDiff]:
        """Run ``<bundle_dir>/install.sh verify --root /`` and return the differences it reports.

        An empty list means the installed platform matches ``bundle_dir``'s manifest exactly.

        Raises:
            PlatformCommandError: the command could not be run or its
                output could not be parsed.
        """
        ...

    def install(self, bundle_dir: Path) -> None:
        """Run ``<bundle_dir>/install.sh install --root /``.

        Raises:
            PlatformCommandError: the command exited non-zero, timed out, or could not start.
        """
        ...

    def record(self, bundle_dir: Path, sha: str) -> None:
        """Run ``<bundle_dir>/install.sh record --root / --sha <sha>``.

        Raises:
            PlatformCommandError: the command exited non-zero, timed out, or could not start.
        """
        ...


class PlatformBundleFetchError(Exception):
    """Raised by :meth:`PlatformBundleSource.fetch` when one of its steps fails.

    ``step`` is one of ``"fetch"``, ``"verify_sha"``, ``"extract"`` --
    mirrors :class:`UpdateStepError`'s shape.
    """

    def __init__(self, step: str, message: str) -> None:
        self.step = step
        super().__init__(f"{step}: {message}")


class PlatformBundleSource(Protocol):
    """Downloads, checksum-verifies, and extracts one platform bundle release.

    Needs no elevated privilege (writes only into a Portal-owned staging
    directory) -- kept separate from :class:`PlatformPort` so ``sudo`` stays
    confined to the three verbs that actually need it.
    """

    def fetch_latest_manifest(self, tag: str) -> PlatformManifest:
        """Fetch and verify just *tag*'s ``.manifest.json`` (+ ``.sha256``) asset -- no tarball.

        Cheap enough to call on every latest-release check
        (:class:`~palmimo_portal.core.platform.PlatformLatestCache`,
        design doc 2.8): ``.fetch()`` downloads and extracts the whole
        bundle, which the latest check has no other use for.

        Raises:
            PlatformBundleFetchError: the asset is absent (older
                ``palmimo-image`` release, no manifest asset published
                yet -- the caller falls back to :meth:`fetch`), could not
                be fetched, or failed checksum/shape validation.
        """
        ...

    def fetch(self, tag: str, dest_dir: Path, on_step: Callable[[str], None]) -> tuple[Path, str]:
        """Fetch *tag*'s bundle into a fresh subdirectory of ``dest_dir``, calling ``on_step`` before each sub-step.

        Sub-steps, in order: ``"fetch"``, ``"verify_sha"``, ``"extract"``.

        Returns:
            The extracted bundle's root directory (containing
            ``install.sh``) and the tarball's verified sha256 hex digest.

        Raises:
            PlatformBundleFetchError: a sub-step failed.
        """
        ...


#: States a :class:`PlatformJob` can be in -- mirrors :data:`UpdateJobState`'s idle/running/done/failed
#: subset (no "checking"/"restarting": a platform job's readiness answer does not depend on Portal
#: itself restarting -- see core/platform.py's module docstring).
PlatformJobState = Literal["idle", "running", "done", "failed"]


@dataclass(frozen=True)
class PlatformJob:
    """One in-flight (or just-finished) platform-bundle update attempt, persisted via :class:`StateStore`.

    ``step`` is one of design doc 2.8's pipeline names (``"fetch"``,
    ``"verify_sha"``, ``"extract"``, ``"preflight"``, ``"install"``,
    ``"verify"``, ``"record"``, ``"restart"``), kept a plain ``str`` (not a
    ``Literal``, mirroring :attr:`UpdateJob.step`) since a ``"preflight"``
    failure encodes extra detail into ``error``
    (``"reflash_required"``/``"portal_too_old"``), not into ``step``.
    """

    state: PlatformJobState
    target_version: int | None
    step: str | None
    error: str | None
    started_at: float | None
    finished_at: float | None


@dataclass(frozen=True)
class PlatformUpdateState:
    """The Portal's whole platform-update picture: just the one in-flight/last job, persisted state-wise."""

    job: PlatformJob


class PlatformLockTimeoutError(Exception):
    """Raised when a platform update is requested while another one is already running.

    Mirrors :class:`AppsLockTimeoutError`/:class:`RunLockTimeoutError`'s
    role for their own axes -- ``api/platform.py`` translates this into 409
    ``platform_update_in_progress``.
    """
