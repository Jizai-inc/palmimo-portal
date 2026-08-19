"""Password setup/verification, session issue/verify, and login rate limiting.

Password hashing is argon2id via ``argon2-cffi``. Sessions are a signed,
timestamped token (``itsdangerous.URLSafeTimedSerializer``) carried in an
``HttpOnly`` / ``SameSite=Strict`` cookie — there is no server-side session
table, so revocation works by rotating the signing key stored alongside the
password hash (see :func:`change_password`), invalidating every previously
issued token at once. The login rate limiter is a fixed, in-memory counter:
5 confirmed-wrong-password failures locks out further attempts for 60
seconds; attempts currently being verified are tracked separately (see
:class:`LoginRateLimiter`) so they cap concurrent guessing without
themselves counting as failures.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from enum import StrEnum

import itsdangerous
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

from palmimo_portal.core.identity import PortalAuthState
from palmimo_portal.ports import (
    IDENTITY_UNAVAILABLE,
    AuthAlreadyExistsError,
    AuthState,
    Identity,
    IdentityUnavailable,
    StateStore,
)


logger = logging.getLogger("palmimo_portal")

SESSION_COOKIE_NAME = "palmimo_session"
SESSION_MAX_AGE_SECONDS = 7 * 24 * 3600
_SESSION_SALT = "palmimo-portal-session"

MAX_LOGIN_FAILURES = 5
LOCKOUT_SECONDS = 60.0

#: How long :meth:`~palmimo_portal.ports.StateStore.lock_auth` waits to
#: acquire its lock before raising :class:`~palmimo_portal.ports.AuthLockTimeoutError`.
#: Shared by :class:`~palmimo_portal.adapters.state.JsonFileStateStore` and
#: :class:`~palmimo_portal.testing.fakes.FakeStateStore` so both port
#: implementations agree on the budget.
AUTH_LOCK_TIMEOUT_SECONDS = 5.0

_hasher = PasswordHasher()


class PasswordAlreadySetError(Exception):
    """Raised by :func:`setup_password` when a password has already been set."""


class PasswordNotSetError(Exception):
    """Raised by :func:`verify_password_against_store` before first-time setup."""


class InvalidCurrentPasswordError(Exception):
    """Raised by :func:`change_password_from_initial`/:func:`change_password_from_full`.

    The ``current_password`` submitted to ``POST /auth/change-password``
    did not match the active credential (the identity file's initial hash
    in initial mode, ``auth.json`` in full mode) -- ``api/auth.py``
    translates this to 401.
    """


def generate_signing_key() -> str:
    """Return a fresh, random session-signing key."""
    return secrets.token_urlsafe(32)


def hash_password(password: str) -> str:
    """Hash a password with argon2id."""
    return _hasher.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """Report whether ``password`` matches the given argon2id hash."""
    try:
        _hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
    return True


def setup_password(store: StateStore, password: str) -> AuthState:
    """Set the Portal password for the first time.

    Goes straight to :meth:`~palmimo_portal.ports.StateStore.create_auth`
    rather than a separate read-then-write, which would let two concurrent
    ``/setup`` requests both pass and the second silently overwrite the
    first. ``create_auth`` makes the check and the write one atomic
    filesystem operation, so exactly one concurrent caller wins.

    Raises:
        PasswordAlreadySetError: a password is already set — use
            :func:`change_password` instead.
    """
    state = AuthState(password_hash=hash_password(password), signing_key=generate_signing_key())
    try:
        store.create_auth(state)
    except AuthAlreadyExistsError as error:
        raise PasswordAlreadySetError() from error
    return state


def change_password(store: StateStore, new_password: str) -> AuthState:
    """Replace the Portal password and rotate the session signing key.

    Rotating the signing key invalidates every session issued under the old
    key — the stateless-session design's answer to revocation.
    """
    state = AuthState(password_hash=hash_password(new_password), signing_key=generate_signing_key())
    store.write_auth(state)
    return state


def change_password_from_initial(
    store: StateStore, identity: Identity, current_password: str, new_password: str
) -> AuthState:
    """Create ``auth.json`` for the first time, verifying ``current_password`` against the sticker hash.

    The initial-mode half of ``POST /auth/change-password``: creating
    ``auth.json`` (via :meth:`~palmimo_portal.ports.StateStore.create_auth`,
    the same ``O_CREAT | O_EXCL`` machinery :func:`setup_password` uses) is
    what "changing" the password means here, so two concurrent requests
    racing from two initial sessions cannot both "succeed" -- the loser
    gets :class:`~palmimo_portal.ports.AuthAlreadyExistsError`.

    The initial-mode signing key is discarded afterwards
    (:meth:`~palmimo_portal.ports.StateStore.discard_initial_signing_key`)
    so a token minted before promotion cannot keep verifying against it if
    the device later returns to :attr:`~palmimo_portal.ports.AuthFileState.ABSENT`.

    Raises:
        InvalidCurrentPasswordError: ``current_password`` does not match
            ``identity.initial_password_hash``.
        AuthAlreadyExistsError: a concurrent change already created
            ``auth.json`` first -- the caller (``api/auth.py``) translates
            this to 409 for the loser of the race.
    """
    if not verify_password(current_password, identity.initial_password_hash):
        raise InvalidCurrentPasswordError()
    state = AuthState(password_hash=hash_password(new_password), signing_key=generate_signing_key())
    store.create_auth(state)
    store.discard_initial_signing_key()
    return state


def change_password_from_full(store: StateStore, current_password: str, new_password: str) -> AuthState:
    """Change an already-set Portal password, verifying ``current_password`` first.

    The full-mode half of ``POST /auth/change-password``: delegates to
    :func:`change_password` only after confirming the caller knows the
    current password -- :func:`change_password` itself does not check
    this, since it is also used internally by callers that already
    authorized the change some other way.

    **Concurrency guarantee**: the verify-then-write sequence runs inside
    :meth:`~palmimo_portal.ports.StateStore.lock_auth`, held for the whole
    call, so two racing callers cannot each verify against the same hash
    and then have the second write clobber the first. The lock is
    blocking, so a contending caller simply waits its turn.

    Raises:
        InvalidCurrentPasswordError: ``current_password`` does not match
            the stored hash.
        PasswordNotSetError: no password has been set yet -- should not
            happen when this is reached with ``auth_state == "set"``.
    """
    with store.lock_auth():
        if not verify_password_against_store(store, current_password):
            raise InvalidCurrentPasswordError()
        return change_password(store, new_password)


def verify_password_against_store(store: StateStore, password: str) -> bool:
    """Verify a login attempt against the stored password hash.

    Raises:
        PasswordNotSetError: no password has been set yet.
    """
    auth = store.read_auth()
    if auth is None:
        raise PasswordNotSetError()
    return verify_password(password, auth.password_hash)


SESSION_MODE_INITIAL = "initial"
SESSION_MODE_FULL = "full"


def issue_session(signing_key: str, mode: str = SESSION_MODE_FULL) -> str:
    """Return a signed, timestamped session token.

    ``mode`` is carried in the payload so :func:`decode_session` (and
    ``app.py``'s ``SessionMiddleware``) can tell an initial-credentials
    session -- issued while ``auth_state == "initial"``, gated to
    change-password and logout only -- from a normal, full session.
    """
    serializer = itsdangerous.URLSafeTimedSerializer(signing_key, salt=_SESSION_SALT)
    token: str = serializer.dumps({"auth": True, "mode": mode})
    return token


def decode_session(signing_key: str, token: str, max_age: int = SESSION_MAX_AGE_SECONDS) -> dict[str, object] | None:
    """Return the session payload if ``token`` is validly signed and not expired, else ``None``."""
    serializer = itsdangerous.URLSafeTimedSerializer(signing_key, salt=_SESSION_SALT)
    try:
        payload: dict[str, object] = serializer.loads(token, max_age=max_age)
    except itsdangerous.BadData:
        return None
    return payload


def verify_session(signing_key: str, token: str, max_age: int = SESSION_MAX_AGE_SECONDS) -> bool:
    """Report whether a session token is validly signed and not expired."""
    return decode_session(signing_key, token, max_age=max_age) is not None


@dataclass
class LoginRateLimiter:
    """Fixed login rate limit: ``max_failures`` in a row locks out for ``lockout_seconds``.

    In-memory and per-process -- the Portal has exactly one account.
    ``clock`` is injectable so tests can control lockout expiry without
    sleeping.

    **Two separate counters.** ``_failures`` counts *confirmed* wrong
    credentials -- the only thing that engages the lockout. ``_pending``
    counts attempts currently being verified (between :meth:`try_attempt`
    reserving a slot and the caller reporting the outcome), so a pending
    attempt does not itself nudge a caller towards the failure threshold
    just for taking time to verify -- otherwise a burst of *correct*
    concurrent logins could lock out even though none of them was wrong.

    **Concurrency guarantee**: every public method acquires :attr:`_lock`
    for its whole body. :meth:`try_attempt` is the atomic check-and-reserve
    primitive callers must use instead of a bare ``is_locked()`` check
    followed by a later verify, which would let N concurrent requests all
    pass through. It denies when already locked out, or when
    ``_failures + _pending >= max_failures`` -- capping in-flight
    verifications to the same budget a sequence of outright failures would
    consume. A caller that wins a reservation must release it via exactly
    one of :meth:`record_failure`, :meth:`record_success`, or
    :meth:`release` -- never left dangling, or the pending count
    permanently overcounts and starves later attempts.

    **Trade-off**: because the cap is ``failures + pending``, a burst of
    more than :attr:`max_failures` simultaneous requests can see a
    transient 429 even though none was ever a wrong password. A human
    typing a password never produces that traffic shape; the cap is what
    stops N browser tabs from guessing passwords in parallel.
    """

    max_failures: int = MAX_LOGIN_FAILURES
    lockout_seconds: float = LOCKOUT_SECONDS
    clock: Callable[[], float] = field(default=time.monotonic)
    _failures: int = field(default=0, init=False, repr=False)
    _pending: int = field(default=0, init=False, repr=False)
    _locked_until: float | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def is_locked(self) -> bool:
        """Report whether login attempts are currently locked out."""
        with self._lock:
            return self._is_locked_locked()

    def _is_locked_locked(self) -> bool:
        """:meth:`is_locked` body for callers already holding :attr:`_lock`."""
        if self._locked_until is None:
            return False
        if self.clock() >= self._locked_until:
            self._locked_until = None
            self._failures = 0
            return False
        return True

    def seconds_remaining(self) -> float:
        """Seconds left in the current lockout, or ``0.0`` if not locked."""
        with self._lock:
            if self._locked_until is None:
                return 0.0
            return max(0.0, self._locked_until - self.clock())

    def record_failure(self) -> None:
        """Resolve one pending reservation as a confirmed wrong credential.

        Decrements :attr:`_pending`, increments :attr:`_failures`, locks
        out once the threshold is hit. Logs a WARNING once per lockout
        *activation*, not once per failed attempt while already locked.
        """
        with self._lock:
            self._pending = max(0, self._pending - 1)
            self._failures += 1
            if self._failures >= self.max_failures:
                self._locked_until = self.clock() + self.lockout_seconds
                self._failures = 0
                logger.warning(
                    "login lockout engaged after %d failures; retrying in %gs",
                    self.max_failures,
                    self.lockout_seconds,
                )

    def record_success(self) -> None:
        """Resolve one pending reservation as a confirmed correct credential.

        Decrements :attr:`_pending`; clears any accumulated failure/lockout.
        """
        with self._lock:
            self._pending = max(0, self._pending - 1)
            self._failures = 0
            self._locked_until = None

    def release(self) -> None:
        """Resolve one pending reservation with no judgment on the credential.

        For an early exit after :meth:`try_attempt` that never verified a
        password (corrupt/unavailable auth state, a resolved race, a lock
        timeout). Decrements :attr:`_pending` only.
        """
        with self._lock:
            self._pending = max(0, self._pending - 1)

    def try_attempt(self) -> bool:
        """Atomically check the lockout and reserve one in-flight verification slot.

        Returns ``False`` if already locked out or
        ``_failures + _pending >= max_failures`` (see the class
        docstring's concurrency guarantee); otherwise increments
        :attr:`_pending` and returns ``True``. Caller MUST resolve the
        reservation exactly once via :meth:`record_failure`,
        :meth:`record_success`, or :meth:`release` -- typically from a
        ``try``/``finally`` starting immediately after success.
        """
        with self._lock:
            if self._is_locked_locked():
                return False
            if self._failures + self._pending >= self.max_failures:
                return False
            self._pending += 1
            return True


#: How long an accepted ``POST /auth/reset`` locks out any further reset --
#: a flat, process-wide throttle on the endpoint itself, not a failure
#: threshold like :data:`LOCKOUT_SECONDS`. See :class:`ResetRateLimiter`.
RESET_LOCKOUT_SECONDS = 60.0


@dataclass
class ResetRateLimiter:
    """Flat process-wide throttle: one accepted ``POST /auth/reset`` per ``lockout_seconds``.

    Not :class:`LoginRateLimiter` generalized to a one-failure threshold:
    a reset has no "wrong password" to fail on -- every accepted
    (``ResetDecision.ALLOW``) reset is itself the event that must be rare.
    :meth:`try_acquire` is called only once ``decide_reset`` has already
    returned ``ALLOW``, never on a denied attempt, so those cannot burn
    the budget a legitimate holder of the sticker password needs. ``clock``
    is injectable so tests can control expiry without sleeping.

    **Per-process, not shared across a fleet.** Exactly one instance lives
    on ``app.state``, and the Portal always runs as a single ``uvicorn``
    worker, so this budget is genuinely process-wide. A restart clears it
    -- acceptable because an accepted reset only ever returns the device
    to its own sticker-gated initial state, never hands control to the
    caller (see :func:`decide_reset`).
    """

    lockout_seconds: float = RESET_LOCKOUT_SECONDS
    clock: Callable[[], float] = field(default=time.monotonic)
    _locked_until: float | None = field(default=None, init=False, repr=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def is_locked(self) -> bool:
        """Report whether a reset is currently throttled."""
        if self._locked_until is None:
            return False
        if self.clock() >= self._locked_until:
            self._locked_until = None
            return False
        return True

    def seconds_remaining(self) -> float:
        """Seconds left in the current throttle window, or ``0.0`` if not locked."""
        if self._locked_until is None:
            return 0.0
        return max(0.0, self._locked_until - self.clock())

    def try_acquire(self) -> bool:
        """Atomically check-and-engage the throttle for ``lockout_seconds``.

        Returns ``True`` (throttle now engaged) if the caller may proceed;
        ``False`` (window untouched) if already throttled. Checking and
        recording happen as one step under :attr:`_lock` -- a separate
        check followed by a later "record it" call would let two
        concurrent requests both observe not-locked and both proceed.
        """
        with self._lock:
            if self.is_locked():
                return False
            self._locked_until = self.clock() + self.lockout_seconds
            return True

    def release(self) -> None:
        """Undo the acquisition :meth:`try_acquire` just granted, reopening the window immediately.

        For when spending the budget turns out to be premature:
        ``POST /auth/reset`` calls this when
        :meth:`~palmimo_portal.ports.StateStore.delete_auth` raises
        *after* :meth:`try_acquire` already returned ``True``. Always
        clears the window outright -- the endpoint holds at most one
        outstanding acquisition at a time, so there is never a prior
        window to restore.
        """
        with self._lock:
            self._locked_until = None


class ResetDecision(StrEnum):
    """The outcome of :func:`decide_reset` -- what ``api/auth.py`` maps onto an HTTP status."""

    ALLOW = "allow"
    DENY_NOT_AVAILABLE = "deny_not_available"
    """Refuse -- 403 ``reset_not_available``. Not an identity-carrying
    device: an unauthenticated reset here would reopen the anonymous
    first-time setup flow -- a takeover, not a nuisance."""
    DENY_UNAVAILABLE = "deny_unavailable"
    """Refuse -- 503 ``identity_unavailable``. The identity read is
    transiently unavailable, so refuse rather than guess, same as
    ``/auth/setup`` and ``/auth/login``."""
    DENY_ALREADY_INITIAL = "deny_already_initial"
    """Refuse -- 409 ``auth_not_set``. Already in ``initial`` mode
    (``auth.json`` absent); nothing left to reset."""


def decide_reset(auth_state: PortalAuthState, identity: Identity | IdentityUnavailable | None) -> ResetDecision:
    """Decide whether ``POST /auth/reset`` may proceed, unauthenticated.

    Takes both ``auth_state`` and the raw ``identity`` read: ``auth_state``
    alone cannot tell an identity-carrying device from a DIY device that
    has finished ``/auth/setup``, since
    :func:`~palmimo_portal.core.identity.compute_auth_state` collapses
    both into :attr:`~palmimo_portal.core.identity.PortalAuthState.SET`
    whenever ``auth.json`` is present. Deciding ALLOW from ``auth_state``
    alone would let an unauthenticated reset on that DIY device reopen the
    anonymous first-time-setup flow -- the takeover this rule exists to
    prevent. ALLOW is only reached when ``auth_state`` is SET/CORRUPT
    *and* ``identity`` is an actual :class:`~palmimo_portal.ports.Identity`.

    Rule, evaluated in this order:

    - :attr:`~palmimo_portal.core.identity.PortalAuthState.UNAVAILABLE`, or
      ``identity is IDENTITY_UNAVAILABLE`` even while ``auth_state`` is
      SET/CORRUPT (a transient read failure, not clean absence -- whether
      this is an identity-carrying device cannot be determined) ->
      :attr:`ResetDecision.DENY_UNAVAILABLE`.
    - :attr:`~palmimo_portal.core.identity.PortalAuthState.INITIAL`
      (already reset, nothing to do) -> :attr:`ResetDecision.DENY_ALREADY_INITIAL`.
    - :attr:`~palmimo_portal.core.identity.PortalAuthState.OPEN_SETUP`, or
      SET/CORRUPT with no identity file at all (a DIY device) ->
      :attr:`ResetDecision.DENY_NOT_AVAILABLE`.
    - SET or CORRUPT with a real identity file -> :attr:`ResetDecision.ALLOW`.
    """
    if auth_state is PortalAuthState.UNAVAILABLE or identity is IDENTITY_UNAVAILABLE:
        return ResetDecision.DENY_UNAVAILABLE
    if auth_state is PortalAuthState.INITIAL:
        return ResetDecision.DENY_ALREADY_INITIAL
    if auth_state is PortalAuthState.OPEN_SETUP:
        return ResetDecision.DENY_NOT_AVAILABLE
    # auth_state is SET or CORRUPT here.
    if isinstance(identity, Identity):
        return ResetDecision.ALLOW
    return ResetDecision.DENY_NOT_AVAILABLE
