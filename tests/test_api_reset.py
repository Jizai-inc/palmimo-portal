"""Tests for ``POST /api/v1/auth/reset``: the unauthenticated login-credentials reset.

Covers the security rule from the design doc: an unauthenticated reset is
allowed only on identity-carrying devices (auth_state ``set`` or
``corrupt``, with an actual identity file present) -- never on a DIY device,
where it would reopen the anonymous first-time-setup flow instead of
returning to a sticker-gated state. See palmimo_portal/core/auth.py's
``decide_reset`` for the full rule, including why it also needs the raw
identity read (not just the auth state) to refuse a DIY device that has
already completed ``/auth/setup``.
"""

from __future__ import annotations

import dataclasses
import json
import logging
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.adapters.identity import FileIdentityStore
from palmimo_portal.core.auth import SESSION_COOKIE_NAME, hash_password
from palmimo_portal.ports import Identity
from palmimo_portal.testing.fakes import FakeAdapterBundle
from palmimo_portal.wiring import AdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}
STICKER_PASSWORD = "sticker-correct-horse"
DEVICE_ID = "palmimo-042"


def _carry_identity(adapters: FakeAdapterBundle, password: str = STICKER_PASSWORD) -> None:
    adapters.identity.identity = Identity(device_id=DEVICE_ID, initial_password_hash=hash_password(password))


def _initial_login(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"password": STICKER_PASSWORD}, headers=CSRF_HEADERS)
    assert response.status_code == 200


def _promote_to_full(client: TestClient) -> None:
    """Log in with the sticker password and change it, promoting the device to auth_state ``set``."""
    _initial_login(client)
    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": STICKER_PASSWORD, "new_password": "owner-password"},
        headers=CSRF_HEADERS,
    )
    assert response.status_code == 200


def test_reset_on_an_identity_device_with_a_set_password_succeeds(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    _promote_to_full(client)

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert response.json() == {"status": "ok", "auth_state": "initial"}
    status = client.get("/api/v1/system/status")
    assert status.json()["auth_state"] == "initial"


def test_reset_clears_the_session_cookie(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)
    _promote_to_full(client)

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert SESSION_COOKIE_NAME not in response.cookies


def test_a_full_session_cookie_issued_before_the_reset_is_rejected_after(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # The signing key lived in auth.json -- deleting it must invalidate
    # every session that was issued under it, immediately.
    _carry_identity(adapters)
    _promote_to_full(client)
    adapters.network.known_networks.add("home")
    old_cookie = client.cookies.get(SESSION_COOKIE_NAME)
    assert old_cookie is not None

    client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    stale_client = TestClient(client.app, cookies={SESSION_COOKIE_NAME: old_cookie})
    stale_client.headers["Host"] = "testserver"
    response = stale_client.get("/api/v1/ssh-keys")
    assert response.status_code == 401


def test_reset_on_an_identity_device_with_a_corrupt_auth_file_succeeds(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    adapters.state.auth_corrupt = True

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert response.json()["auth_state"] == "initial"


def test_reset_deletes_a_corrupt_auth_file_too(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)
    adapters.state.auth_corrupt = True

    client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    from palmimo_portal.ports import AuthFileState

    assert adapters.state.auth_state() is AuthFileState.ABSENT


def test_reset_logs_a_warning_with_the_client_address(
    client: TestClient, adapters: FakeAdapterBundle, caplog: pytest.LogCaptureFixture
) -> None:
    _carry_identity(adapters)
    _promote_to_full(client)

    with caplog.at_level(logging.WARNING):
        response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert any(
        record.levelno == logging.WARNING and "login credentials reset" in record.message for record in caplog.records
    )
    assert DEVICE_ID in caplog.text


def test_reset_works_while_unprovisioned(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # The whole point of the feature: a forgotten password blocks the Wi-Fi
    # setup flow itself on an identity device, so reset must work before
    # Wi-Fi is ever configured.
    _carry_identity(adapters)
    _promote_to_full(client)
    assert not adapters.network.known_networks

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 200


def test_reset_is_403_on_a_diy_device_with_no_password_set(client: TestClient, adapters: FakeAdapterBundle) -> None:
    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "reset_not_available"


def test_reset_is_403_on_a_diy_device_that_has_completed_setup(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # The security gap this closes: PortalAuthState.SET is reached by both
    # identity-carrying devices and DIY devices that finished /auth/setup --
    # auth_state alone cannot tell them apart (compute_auth_state's
    # docstring). Allowing this would delete the DIY device's only
    # credential and drop it back to open_setup (identity is None),
    # reopening the anonymous first-time-setup flow -- a takeover.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    from palmimo_portal.ports import AuthState

    before = adapters.state.read_auth()
    assert before is not None

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "reset_not_available"
    assert adapters.state.read_auth() == before
    assert isinstance(before, AuthState)


def test_reset_does_not_touch_auth_json_on_a_diy_device(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    before = adapters.state.read_auth()

    client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert adapters.state.read_auth() == before


def test_reset_is_409_when_already_initial(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "auth_not_set"


def test_reset_is_503_when_identity_is_unavailable_and_no_password_is_set(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    adapters.identity.unavailable = True

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_unavailable"


def test_reset_is_503_when_identity_is_unavailable_even_with_a_password_set(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # A set auth.json alone cannot tell an identity device from a DIY one
    # (see the SET/DIY test above) -- when the identity read cannot resolve
    # that ambiguity, refuse rather than guess either way.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.identity.unavailable = True

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_unavailable"


def test_reset_refuses_when_a_cached_identity_has_since_been_removed_from_disk(
    tmp_path: Path, app: FastAPI, adapters: FakeAdapterBundle, client: TestClient
) -> None:
    # Reproduces the exact scenario FileIdentityStore.read_identity()'s
    # caching would get wrong: an identity-carrying device is promoted to a
    # full session (every check on the way primes the cache with a real
    # Identity), and only afterwards does the identity file disappear from
    # disk. A reset decided from the stale cache would wrongly ALLOW and
    # delete the real auth.json -- the fix (read_identity_uncached) must
    # instead see the file is gone and refuse it exactly like a DIY device
    # that completed /auth/setup (see decide_reset's docstring).
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(
        json.dumps({"device_id": DEVICE_ID, "initial_password_hash": hash_password(STICKER_PASSWORD)})
    )
    identity_store = FileIdentityStore(identity_path)
    # A deliberate mixed bundle -- see test_api_system.py's equivalent test.
    app.state.adapters = dataclasses.replace(cast(AdapterBundle, adapters), identity=identity_store)

    _initial_login(client)
    change_response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": STICKER_PASSWORD, "new_password": "owner-password"},
        headers=CSRF_HEADERS,
    )
    assert change_response.status_code == 200
    before = adapters.state.read_auth()
    assert before is not None
    assert identity_store.read_identity() is not None  # the cache is now primed with a real Identity

    identity_path.unlink()  # the file is gone, but the in-process cache still holds the old Identity

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "reset_not_available"
    assert adapters.state.read_auth() == before


def test_a_failing_delete_auth_does_not_burn_the_rate_limit_budget(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    _promote_to_full(client)
    adapters.state.raise_on_delete_auth = RuntimeError("disk full")

    # Starlette's ServerErrorMiddleware re-raises after sending the 500
    # response it builds from the registered Exception handler -- the
    # TestClient (raise_server_exceptions=True, the default) surfaces that
    # re-raise directly rather than swallowing it, so the exception itself
    # is what proves the request actually reached delete_auth() and failed.
    with pytest.raises(RuntimeError, match="disk full"):
        client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)
    assert adapters.state.read_auth() is not None  # delete_auth raised before mutating anything

    adapters.state.raise_on_delete_auth = None
    second = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert second.status_code == 200


def test_reset_maps_a_lock_timeout_to_409(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # A concurrent password change already holding StateStore.lock_auth()
    # past its own timeout must surface as a distinct, retryable 409 --
    # matching change-password's own mapping of the same error -- not a
    # bare 500, and must release the rate-limit budget just like any other
    # failing delete_auth().
    from palmimo_portal.ports import AuthLockTimeoutError

    _carry_identity(adapters)
    _promote_to_full(client)
    adapters.state.raise_on_delete_auth = AuthLockTimeoutError()

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "auth_change_in_progress"
    assert adapters.state.read_auth() is not None  # delete_auth raised before mutating anything

    adapters.state.raise_on_delete_auth = None
    second = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert second.status_code == 200  # budget was released, not burned


def test_a_second_reset_within_60_seconds_is_rate_limited(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)
    _promote_to_full(client)
    first = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)
    assert first.status_code == 200

    # The device is back in "initial" mode -- log in with the sticker again
    # and promote it, to get back to a resettable "set" state, and confirm
    # the *throttle* (not decide_reset) is what blocks the second call.
    _promote_to_full(client)

    second = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert second.status_code == 429
    assert second.json()["error"]["code"] == "auth_rate_limited"
    assert "retry_after_seconds" in second.json()["error"]["params"]


def test_a_denied_reset_attempt_does_not_burn_the_rate_limit_budget(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # A DIY device's refused attempts must not be able to lock a legitimate
    # identity-carrying device's own reset budget -- they are separate
    # requests against the same process-wide limiter, so this also confirms
    # a denied (non-ALLOW) outcome never calls ResetRateLimiter.try_acquire().
    denied = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)
    assert denied.status_code == 403

    _carry_identity(adapters)
    _promote_to_full(client)
    allowed = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert allowed.status_code == 200


def test_reset_requires_no_csrf_bypass_but_no_session_either(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)
    _promote_to_full(client)
    client.cookies.delete(SESSION_COOKIE_NAME)

    response = client.post("/api/v1/auth/reset", headers=CSRF_HEADERS)

    assert response.status_code == 200


def test_reset_without_the_csrf_header_is_rejected(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)
    _promote_to_full(client)

    response = client.post("/api/v1/auth/reset")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "csrf_header_missing"
