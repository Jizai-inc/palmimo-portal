"""Password setup/verification, session issue/verify, and login rate limiting.

Passwords hash with argon2id. Sessions are a signed, timestamped token
(``itsdangerous.URLSafeTimedSerializer``) in an ``HttpOnly`` /
``SameSite=Strict`` cookie — no server-side session table, so revocation
rotates the signing key stored with the password hash (see
:func:`change_password`), invalidating every issued token at once.
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

    Uses :meth:`~palmimo_portal.ports.StateStore.create_auth` (atomic
    check-and-write), not a read-then-write, so two concurrent ``/setup``
    requests cannot both pass with the second silently overwriting the first.

    Raises:
        PasswordAlreadySetError: a password is already set — use :func:`change_password` instead.
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

    The initial-mode half of ``POST /auth/change-password``: uses
    :meth:`~palmimo_portal.ports.StateStore.create_auth` (same
    exclusive-create as :func:`setup_password`), so two concurrent requests
    from two initial sessions cannot both succeed -- the loser gets
    :class:`~palmimo_portal.ports.AuthAlreadyExistsError`. Discards the
    initial-mode signing key afterwards so a token minted before promotion
    cannot keep verifying if the device later returns to
    :attr:`~palmimo_portal.ports.AuthFileState.ABSENT`.

    Raises:
        InvalidCurrentPasswordError: ``current_password`` does not match ``identity.initial_password_hash``.
        AuthAlreadyExistsError: a concurrent change created ``auth.json`` first;
            ``api/auth.py`` translates this to 409 for the loser.
    """
    if not verify_password(current_password, identity.initial_password_hash):
        raise InvalidCurrentPasswordError()
    state = AuthState(password_hash=hash_password(new_password), signing_key=generate_signing_key())
    store.create_auth(state)
    store.discard_initial_signing_key()
    return state


def change_password_from_full(store: StateStore, current_password: str, new_password: str) -> AuthState:
    """Change an already-set Portal password, verifying ``current_password`` first.

    The full-mode half of ``POST /auth/change-password``: :func:`change_password`
    itself does not check the current password (also used internally by
    already-authorized callers), so this verifies first.

    Concurrency: the verify-then-write sequence runs inside
    :meth:`~palmimo_portal.ports.StateStore.lock_auth`, held for the whole
    call, so two racing callers cannot both verify against the same hash
    and have the second write clobber the first.

    Raises:
        InvalidCurrentPasswordError: ``current_password`` does not match the stored hash.
        PasswordNotSetError: no password has been set yet.
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

    In-memory and per-process -- the Portal has exactly one account. ``clock``
    is injectable so tests can control lockout expiry without sleeping.

    Two counters: ``_failures`` counts *confirmed* wrong credentials (the
    only thing that engages lockout); ``_pending`` counts attempts currently
    being verified, so a pending attempt does not itself count toward the
    threshold -- otherwise a burst of *correct* concurrent logins could
    lock out even though none was wrong.

    Concurrency: every public method holds :attr:`_lock` for its whole
    body. :meth:`try_attempt` is the atomic check-and-reserve primitive
    callers must use instead of a bare ``is_locked()`` check followed by a
    later verify, which would let N concurrent requests all pass through;
    it denies when ``_failures + _pending >= max_failures``, capping
    in-flight verifications to the same budget outright failures would
    consume. A caller that wins a reservation must release it via exactly
    one of :meth:`record_failure`, :meth:`record_success`, or
    :meth:`release` -- never left dangling, or ``_pending`` permanently
    overcounts and starves later attempts. Trade-off: a burst of more than
    ``max_failures`` simultaneous requests can see a transient 429 even
    with no wrong password involved -- a human typing never produces that
    traffic shape; the cap stops N tabs guessing in parallel.
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
        """Resolve one pending reservation as a confirmed wrong credential; locks out once the threshold is hit.

        Logs a WARNING once per lockout *activation*, not once per failed attempt while already locked.
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
        """Resolve one pending reservation as a confirmed correct credential; clears any accumulated lockout."""
        with self._lock:
            self._pending = max(0, self._pending - 1)
            self._failures = 0
            self._locked_until = None

    def release(self) -> None:
        """Resolve one pending reservation with no judgment on the credential (early exit that never verified)."""
        with self._lock:
            self._pending = max(0, self._pending - 1)

    def try_attempt(self) -> bool:
        """Atomically check the lockout and reserve one in-flight verification slot; see class docstring.

        Caller MUST resolve the reservation exactly once via
        :meth:`record_failure`, :meth:`record_success`, or :meth:`release`.
        """
        with self._lock:
            if self._is_locked_locked():
                return False
            if self._failures + self._pending >= self.max_failures:
                return False
            self._pending += 1
            return True


#: Flat, process-wide throttle on accepted ``POST /auth/reset`` -- not a
#: failure threshold like :data:`LOCKOUT_SECONDS`. See :class:`ResetRateLimiter`.
RESET_LOCKOUT_SECONDS = 60.0


@dataclass
class ResetRateLimiter:
    """Flat process-wide throttle: one accepted ``POST /auth/reset`` per ``lockout_seconds``.

    Not :class:`LoginRateLimiter` generalized: a reset has no "wrong
    password" to fail on, so every accepted (``ResetDecision.ALLOW``)
    reset is itself the event that must be rare. :meth:`try_acquire` is
    called only once ``decide_reset`` has returned ``ALLOW``, never on a
    denied attempt, so those cannot burn the legitimate budget. ``clock``
    is injectable for tests. Per-process only (single ``uvicorn`` worker,
    one instance on ``app.state``); a restart clearing it is acceptable
    because an accepted reset only returns the device to its own
    sticker-gated initial state, never hands control to the caller.
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

        Checking and recording happen as one step under :attr:`_lock` -- a
        separate check followed by a later "record it" call would let two
        concurrent requests both observe not-locked and both proceed.
        """
        with self._lock:
            if self.is_locked():
                return False
            self._locked_until = self.clock() + self.lockout_seconds
            return True

    def release(self) -> None:
        """Undo the acquisition :meth:`try_acquire` just granted, reopening the window immediately.

        For when spending the budget turns out premature: ``POST
        /auth/reset`` calls this when
        :meth:`~palmimo_portal.ports.StateStore.delete_auth` raises after
        :meth:`try_acquire` already returned ``True``.
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
    finished ``/auth/setup``, since
    :func:`~palmimo_portal.core.identity.compute_auth_state` collapses both
    into :attr:`~palmimo_portal.core.identity.PortalAuthState.SET` whenever
    ``auth.json`` is present -- deciding ALLOW from ``auth_state`` alone
    would let an unauthenticated reset reopen the anonymous first-time-setup
    flow on a DIY device, the takeover this rule prevents. ALLOW requires
    ``auth_state`` SET/CORRUPT *and* a real :class:`~palmimo_portal.ports.Identity`.

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
