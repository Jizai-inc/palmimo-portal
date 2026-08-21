"""``/api/v1/auth``: first-time setup, login, change-password, and logout.

Two first-time-owner flows (palmimo-portal-technical.md's Authentication
section):

- **DIY / open-setup** (no identity file): ``POST /setup`` sets the
  password once, unauthenticated.
- **Identity-carrying device**: ``POST /setup`` is always 409. ``POST
  /login`` with the sticker (initial) password issues a session flagged
  ``mode="initial"`` -- gated to ``POST /change-password`` and ``POST
  /logout`` only (see :func:`~palmimo_portal.api.deps.require_full_session`,
  applied to every other router) -- and ``POST /change-password`` creates
  ``auth.json``, transitioning to normal (``mode="full"``) operation.
"""

from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel

from palmimo_portal.api.deps import (
    get_identity_store,
    get_rate_limiter,
    get_reset_rate_limiter,
    get_state_store,
    require_auth,
    require_provisioned_unless_identity,
)
from palmimo_portal.api.errors import PortalError
from palmimo_portal.core.auth import (
    SESSION_COOKIE_NAME,
    SESSION_MAX_AGE_SECONDS,
    SESSION_MODE_FULL,
    SESSION_MODE_INITIAL,
    InvalidCurrentPasswordError,
    LoginRateLimiter,
    PasswordAlreadySetError,
    PasswordNotSetError,
    ResetDecision,
    ResetRateLimiter,
    change_password_from_full,
    change_password_from_initial,
    decide_reset,
    issue_session,
    setup_password,
    verify_identity_password,
    verify_password_against_store,
)
from palmimo_portal.core.identity import PortalAuthState, compute_auth_state
from palmimo_portal.ports import (
    IDENTITY_UNAVAILABLE,
    AuthAlreadyExistsError,
    AuthFileState,
    AuthLockTimeoutError,
    Identity,
    IdentityStore,
    StateStore,
)


logger = logging.getLogger("palmimo_portal")


router = APIRouter(prefix="/api/v1/auth", tags=["auth"])


class PasswordRequest(BaseModel):
    password: str


class ChangePasswordRequest(BaseModel):
    current_password: str | None = None
    new_password: str


class StatusResponse(BaseModel):
    status: str = "ok"


class LoginResponse(BaseModel):
    status: str = "ok"
    mode: str


class ChangePasswordResponse(BaseModel):
    status: str = "ok"
    mode: str = SESSION_MODE_FULL


class ResetResponse(BaseModel):
    status: str = "ok"
    auth_state: str = "initial"


def _set_session_cookie(response: Response, signing_key: str, mode: str) -> None:
    token = issue_session(signing_key, mode=mode)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=token,
        max_age=SESSION_MAX_AGE_SECONDS,
        httponly=True,
        samesite="strict",
    )


@router.post("/setup")
def setup(
    body: PasswordRequest,
    state: StateStore = Depends(get_state_store),
    identity_store: IdentityStore = Depends(get_identity_store),
) -> StatusResponse:
    """Set the Portal password for the first time -- the DIY (open-setup) flow only.

    Reachable while unprovisioned and requires no session of its own.

    Raises:
        PortalError: 409 ``auth_state_corrupt`` if ``auth.json`` exists but
            cannot be read -- refused rather than treated as unset, so a
            crash-corrupted auth file can't reopen an already-owned device
            to anyone on the LAN; 503 ``identity_unavailable`` if the
            identity file could not be read at all -- a transient error
            (e.g. ``/boot/firmware`` not mounted yet) distinct from clean
            absence, refused rather than treated as "no identity file" so
            this unauthenticated flow can't open on a sticker/OEM device
            for as long as the read keeps failing; 409
            ``initial_credentials_required`` if this device carries a
            manufacturing identity file -- it must go through
            ``POST /login`` with the sticker password and then
            ``POST /change-password`` instead, regardless of whether
            ``auth.json`` happens to be set; 409 ``auth_already_set`` if a
            password is already set.
    """
    if state.auth_state() is AuthFileState.CORRUPT:
        raise PortalError(409, "auth_state_corrupt")
    identity = identity_store.read_identity()
    if identity is IDENTITY_UNAVAILABLE:
        raise PortalError(503, "identity_unavailable")
    if identity is not None:
        raise PortalError(409, "initial_credentials_required")
    try:
        setup_password(state, body.password)
    except PasswordAlreadySetError as error:
        raise PortalError(409, "auth_already_set") from error
    return StatusResponse()


@router.post("/login", dependencies=[Depends(require_provisioned_unless_identity)])
def login(
    body: PasswordRequest,
    response: Response,
    state: StateStore = Depends(get_state_store),
    identity_store: IdentityStore = Depends(get_identity_store),
    rate_limiter: LoginRateLimiter = Depends(get_rate_limiter),
) -> LoginResponse:
    """Verify the password and, on success, issue a session cookie.

    Checks against ``auth.json`` (mode ``"full"``) when a password has been
    set, or the identity file's sticker password (mode ``"initial"``) when one
    hasn't but an identity file is present -- see
    :func:`~palmimo_portal.core.identity.compute_auth_state`. Response
    ``mode`` lets the frontend route straight to change-password after an
    initial-mode login. Fixed rate limit: 5 failures locks attempts out for
    60s (see :class:`~palmimo_portal.core.auth.LoginRateLimiter`), regardless
    of which mode was checked.

    Raises:
        PortalError: 409 ``auth_state_corrupt`` if ``auth.json`` exists but
            cannot be read (see :func:`setup`); 429 ``auth_rate_limited``
            while locked out; 401 ``invalid_credentials`` on a wrong
            password; 409 ``auth_not_set`` if no password has been set yet
            and no identity file is present either (the DIY out-of-box
            state -- ``POST /setup`` first), or if ``auth.json`` is
            deleted between the classification above and the verify call
            running (a race, not reachable from a normal request); 503
            ``identity_unavailable`` if the identity file could not be
            read (see :func:`setup`).
    """
    identity = identity_store.read_identity()
    portal_state = compute_auth_state(state.auth_state(), identity)
    if portal_state is PortalAuthState.CORRUPT:
        raise PortalError(409, "auth_state_corrupt")
    if portal_state is PortalAuthState.UNAVAILABLE:
        raise PortalError(503, "identity_unavailable")

    # Mode must resolve before try_attempt(): a DIY device with no password
    # set yet must not burn rate-limit budget here.
    if portal_state is PortalAuthState.SET:
        mode = SESSION_MODE_FULL
    elif portal_state is PortalAuthState.INITIAL:
        assert identity is not None  # compute_auth_state only returns INITIAL when identity is not None
        mode = SESSION_MODE_INITIAL
    else:
        raise PortalError(409, "auth_not_set")

    if not rate_limiter.try_attempt():
        raise PortalError(429, "auth_rate_limited", retry_after_seconds=rate_limiter.seconds_remaining())

    # Every path MUST resolve the try_attempt() reservation exactly once:
    # record_failure/record_success on a judged credential, else release().
    outcome_recorded = False
    try:
        if mode == SESSION_MODE_FULL:
            try:
                correct = verify_password_against_store(state, body.password)
            except PasswordNotSetError as error:
                # auth.json deleted between the auth_state() check and this call (a race).
                raise PortalError(409, "auth_not_set") from error
        else:
            assert identity is not None and identity is not IDENTITY_UNAVAILABLE
            correct = verify_identity_password(body.password, identity)

        if not correct:
            rate_limiter.record_failure()
            outcome_recorded = True
            raise PortalError(401, "invalid_credentials")

        rate_limiter.record_success()
        outcome_recorded = True
    finally:
        if not outcome_recorded:
            rate_limiter.release()

    if mode == SESSION_MODE_FULL:
        auth = state.read_auth()
        assert auth is not None  # verify_password_against_store already confirmed this
        signing_key = auth.signing_key
    else:
        signing_key = state.read_or_create_initial_signing_key()
    _set_session_cookie(response, signing_key, mode)
    return LoginResponse(mode=mode)


@router.post(
    "/change-password",
    dependencies=[Depends(require_provisioned_unless_identity), Depends(require_auth)],
)
def change_password_endpoint(
    body: ChangePasswordRequest,
    request: Request,
    response: Response,
    state: StateStore = Depends(get_state_store),
    identity_store: IdentityStore = Depends(get_identity_store),
    rate_limiter: LoginRateLimiter = Depends(get_rate_limiter),
) -> ChangePasswordResponse:
    """Change the Portal password, from either an initial or a full session.

    This and ``POST /logout`` are the only authenticated endpoints reachable
    while the session's mode is ``"initial"`` --
    :func:`~palmimo_portal.api.deps.require_full_session`, applied
    everywhere else, is deliberately not applied here.

    - **From an initial session**: no ``current_password`` check --
      ``auth.json`` is *created* for the first time
      (:func:`~palmimo_portal.core.auth.change_password_from_initial`, same
      exclusive-create machinery as ``POST /setup``, so two concurrent
      requests from two initial sessions cannot both win). The login rate
      limiter is not touched on this path: an initial session already
      proved sticker-password knowledge at login, the sticker password
      crosses the same plain-HTTP LAN hop either way, and this endpoint is
      the only action an initial session can take -- re-verifying here
      would only let a stolen initial-mode cookie burn the shared login
      budget.
    - **From a full session**: ``current_password`` checks against the
      stored hash and shares :class:`~palmimo_portal.core.auth.LoginRateLimiter`
      with ``POST /login`` (same instance, budget, lockout) -- a stolen
      full session cookie must not hand an attacker an unlimited oracle to
      brute-force the current password; ``auth.json`` is rotated in place
      (:func:`~palmimo_portal.core.auth.change_password_from_full`).

    Either way a fresh full-mode session is issued, so the caller need not
    log in again -- required for the initial-to-full transition, since
    Wi-Fi endpoints are session-gated even while unprovisioned on an
    identity-carrying device (see
    :func:`~palmimo_portal.api.deps.require_wifi_access`).

    Raises:
        PortalError: 409 ``auth_state_corrupt`` if ``auth.json`` is corrupt,
            or (initial session only) if the identity file backing the
            session has since disappeared; 503 ``identity_unavailable`` if
            the identity file could not be read (initial session only);
            409 ``auth_change_conflict`` if a concurrent change from
            another initial session already won the race to create
            ``auth.json`` (initial session only); 401
            ``invalid_current_password`` (full session only) if
            ``current_password`` is missing or wrong -- checked before
            ``try_attempt()`` when missing, so a malformed request doesn't
            spend budget; 429 ``auth_rate_limited`` while locked out (full
            session only); 409 ``auth_not_set`` (full session only) if
            ``auth.json`` is deleted between the mode check and the verify
            call running (a race, not reachable from a normal request);
            409 ``auth_change_in_progress`` (full session only) if a
            concurrent full-mode change is already holding
            :meth:`~palmimo_portal.ports.StateStore.lock_auth` past its
            timeout.
    """
    if state.auth_state() is AuthFileState.CORRUPT:
        raise PortalError(409, "auth_state_corrupt")

    session_mode = getattr(request.state, "session_mode", None)
    if session_mode == SESSION_MODE_INITIAL:
        maybe_identity = identity_store.read_identity()
        if maybe_identity is IDENTITY_UNAVAILABLE:
            # Transient read failure, not clean absence.
            raise PortalError(503, "identity_unavailable")
        if maybe_identity is None:
            # Session claims initial mode but its identity file is gone.
            raise PortalError(409, "auth_state_corrupt")

        try:
            new_state = change_password_from_initial(state, body.new_password)
        except AuthAlreadyExistsError as error:
            raise PortalError(409, "auth_change_conflict") from error

        _set_session_cookie(response, new_state.signing_key, SESSION_MODE_FULL)
        return ChangePasswordResponse()

    if body.current_password is None:
        raise PortalError(401, "invalid_current_password")

    if not rate_limiter.try_attempt():
        raise PortalError(429, "auth_rate_limited", retry_after_seconds=rate_limiter.seconds_remaining())

    # Every path MUST resolve the try_attempt() reservation exactly once.
    outcome_recorded = False
    try:
        try:
            new_state = change_password_from_full(state, body.current_password, body.new_password)
        except InvalidCurrentPasswordError as error:
            rate_limiter.record_failure()
            outcome_recorded = True
            raise PortalError(401, "invalid_current_password") from error
        except PasswordNotSetError as error:
            raise PortalError(409, "auth_not_set") from error
        except AuthLockTimeoutError as error:
            raise PortalError(409, "auth_change_in_progress") from error
        rate_limiter.record_success()
        outcome_recorded = True
    finally:
        if not outcome_recorded:
            rate_limiter.release()

    _set_session_cookie(response, new_state.signing_key, SESSION_MODE_FULL)
    return ChangePasswordResponse()


@router.post("/logout", dependencies=[Depends(require_provisioned_unless_identity), Depends(require_auth)])
def logout(response: Response) -> StatusResponse:
    """Clear the session cookie.

    Cookie deletion only, not server-side revocation -- there is no session
    table; a session is a signed, timestamped token verified purely from the
    signing key. A token copied off this browser before logout stays valid
    until it expires (:data:`SESSION_MAX_AGE_SECONDS`) or the signing key
    rotates (only :func:`change_password` does that). Deliberate trade-off:
    no server-side session store on a single-account device with no
    database of its own.
    """
    response.delete_cookie(SESSION_COOKIE_NAME)
    return StatusResponse()


@router.post("/reset")
def reset(
    request: Request,
    response: Response,
    state: StateStore = Depends(get_state_store),
    identity_store: IdentityStore = Depends(get_identity_store),
    reset_rate_limiter: ResetRateLimiter = Depends(get_reset_rate_limiter),
) -> ResetResponse:
    """Reset the Portal's login credentials -- unauthenticated, identity-carrying devices only.

    Deliberately carries **no** ``require_auth``/``require_provisioned_unless_identity``,
    unlike every other endpoint here: a forgotten owner password otherwise
    blocks the Wi-Fi setup flow itself (Wi-Fi is session-gated the moment an
    identity file exists -- :func:`~palmimo_portal.api.deps.require_wifi_access`),
    so a reset must work before Wi-Fi is configured and without a session.
    The DIY case is refused by :func:`~palmimo_portal.core.auth.decide_reset`
    itself.

    This is the one unauthenticated state-changing action in the API --
    anyone on the LAN can trigger it on an identity-carrying device, by
    design, as a bounded nuisance/DoS (device lands back in ``initial``
    mode, gated to the sticker password, never handed to the caller).
    Every accepted reset is logged at WARNING with the caller's address,
    and throttled process-wide to one per
    :data:`~palmimo_portal.core.auth.RESET_LOCKOUT_SECONDS` via
    :meth:`~palmimo_portal.core.auth.ResetRateLimiter.try_acquire` (separate
    budget from :class:`~palmimo_portal.core.auth.LoginRateLimiter`, engaged
    only on an *accepted* reset, released again if
    :meth:`~palmimo_portal.ports.StateStore.delete_auth` then fails).

    Reads identity via :meth:`~palmimo_portal.ports.IdentityStore.read_identity_uncached`,
    not the cached read every other endpoint uses -- this decision must
    never be made from a stale cache of an identity file since removed.

    Raises:
        PortalError: 429 ``auth_rate_limited`` while throttled -- checked
            twice: a cheap non-atomic peek via
            :meth:`~palmimo_portal.core.auth.ResetRateLimiter.is_locked`
            before the identity/auth-state read, and again via
            :meth:`~palmimo_portal.core.auth.ResetRateLimiter.try_acquire`
            right before the delete, which atomically spends the budget;
            403 ``reset_not_available`` on a DIY device (no identity
            file); 503 ``identity_unavailable`` if the identity file
            could not be read (see :func:`setup`); 409 ``auth_not_set`` if
            the device is already in ``initial`` mode (nothing to reset);
            409 ``auth_change_in_progress`` if a concurrent password
            change is already holding
            :meth:`~palmimo_portal.ports.StateStore.lock_auth` past its
            timeout.
    """
    if reset_rate_limiter.is_locked():
        raise PortalError(429, "auth_rate_limited", retry_after_seconds=reset_rate_limiter.seconds_remaining())

    identity = identity_store.read_identity_uncached()
    portal_state = compute_auth_state(state.auth_state(), identity)
    decision = decide_reset(portal_state, identity)
    if decision is ResetDecision.DENY_NOT_AVAILABLE:
        raise PortalError(403, "reset_not_available")
    if decision is ResetDecision.DENY_UNAVAILABLE:
        raise PortalError(503, "identity_unavailable")
    if decision is ResetDecision.DENY_ALREADY_INITIAL:
        raise PortalError(409, "auth_not_set")

    if not reset_rate_limiter.try_acquire():
        raise PortalError(429, "auth_rate_limited", retry_after_seconds=reset_rate_limiter.seconds_remaining())

    client_host = request.client.host if request.client is not None else "unknown"
    device_id = identity.device_id if isinstance(identity, Identity) else None
    logger.warning("auth: login credentials reset requested from %s (device %s)", client_host, device_id)
    try:
        state.delete_auth()
    except AuthLockTimeoutError as error:
        reset_rate_limiter.release()
        raise PortalError(409, "auth_change_in_progress") from error
    except Exception:
        reset_rate_limiter.release()
        raise
    response.delete_cookie(SESSION_COOKIE_NAME)
    return ResetResponse()
