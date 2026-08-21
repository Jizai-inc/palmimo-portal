"""Tests for ``/api/v1/auth``: setup, login, logout, and the rate limit."""

from __future__ import annotations

import contextlib
import json
from collections.abc import Iterator

import httpx
import pytest
from starlette.testclient import TestClient

from palmimo_portal.core.auth import SESSION_COOKIE_NAME
from palmimo_portal.ports import AuthLockTimeoutError
from palmimo_portal.testing.fakes import FakeAdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


def _provision(adapters: FakeAdapterBundle) -> None:
    adapters.network.known_networks.add("home")


def _post_json_with_lone_surrogate(client: TestClient, url: str, payload: dict[str, str]) -> httpx.Response:
    # httpx's own json= encoding (ensure_ascii=False) would try to UTF-8
    # encode the raw surrogate character while building the request body,
    # failing before the request is even sent -- json.dumps's ensure_ascii
    # default instead emits the literal `\ud800` escape sequence (pure
    # ASCII on the wire), letting the server-side json.loads() decode it
    # back into an in-memory surrogate, reproducing what a real attacker's
    # raw HTTP request body would contain.
    body = json.dumps(payload).encode("ascii")
    headers = {**CSRF_HEADERS, "Content-Type": "application/json"}
    return client.post(url, content=body, headers=headers)


def test_setup_then_setup_again_is_rejected(client: TestClient) -> None:
    first = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    second = client.post("/api/v1/auth/setup", json={"password": "other"}, headers=CSRF_HEADERS)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "auth_already_set"


def test_login_sets_a_session_cookie(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)

    response = client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert SESSION_COOKIE_NAME in response.cookies


def test_login_with_the_wrong_password_is_rejected(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)

    response = client.post("/api/v1/auth/login", json={"password": "wrong"}, headers=CSRF_HEADERS)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"
    assert SESSION_COOKIE_NAME not in response.cookies


def test_logout_clears_the_session(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    logout_response = client.post("/api/v1/auth/logout", headers=CSRF_HEADERS)
    assert logout_response.status_code == 200

    protected = client.get("/api/v1/ssh-keys")
    assert protected.status_code == 401


def test_protected_endpoint_requires_a_session(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _provision(adapters)

    response = client.get("/api/v1/ssh-keys")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_login_with_an_unencodable_password_is_rejected_not_500(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # A lone UTF-16 surrogate is valid JSON (json.loads accepts it) but
    # cannot be UTF-8 encoded -- must be treated as simply the wrong
    # password, not surface argon2-cffi's UnicodeEncodeError as a 500.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)

    response = _post_json_with_lone_surrogate(client, "/api/v1/auth/login", {"password": "\ud800"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_with_an_unencodable_password_burns_rate_limit_budget(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)

    for _ in range(5):
        failed = _post_json_with_lone_surrogate(client, "/api/v1/auth/login", {"password": "\ud800"})
        assert failed.status_code == 401

    locked = client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "auth_rate_limited"


def test_login_locks_out_after_five_failures(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)

    for _ in range(5):
        failed = client.post("/api/v1/auth/login", json={"password": "wrong"}, headers=CSRF_HEADERS)
        assert failed.status_code == 401

    locked = client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "auth_rate_limited"


def test_setup_is_409_when_auth_state_is_corrupt(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.state.auth_corrupt = True

    response = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "auth_state_corrupt"


def test_login_is_409_when_auth_state_is_corrupt(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # A password was set, then the file became unreadable (e.g. a crash
    # mid-write) -- login must not fall through to "no password set" and
    # must not accept a guess either, both of which read().is None would
    # otherwise make indistinguishable from the legitimate unset case.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    adapters.state.auth_corrupt = True

    response = client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "auth_state_corrupt"


def test_setup_works_again_after_the_corrupt_file_is_deleted(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.state.auth_corrupt = True
    corrupt_response = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    assert corrupt_response.status_code == 409

    # The only recovery path: an operator deletes auth.json over SSH,
    # returning it to ABSENT.
    adapters.state.auth_corrupt = False

    response = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 200


def test_a_session_issued_before_a_password_change_is_rejected_after(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    from palmimo_portal.core.auth import change_password

    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    assert client.get("/api/v1/ssh-keys").status_code == 200

    change_password(adapters.state, "new-password")

    response = client.get("/api/v1/ssh-keys")
    assert response.status_code == 401


def test_login_is_409_when_auth_json_is_deleted_mid_request(
    client: TestClient, adapters: FakeAdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # auth_state() (checked first, classifying this as "set") and the
    # actual verify call can observe different states if auth.json is
    # deleted in between -- verify_password_against_store then raises
    # PasswordNotSetError, which must not surface as an unhandled 500.
    from palmimo_portal.core import auth as auth_core

    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)

    def raise_password_not_set(store: object, password: str) -> bool:
        raise auth_core.PasswordNotSetError()

    monkeypatch.setattr(auth_core, "verify_password_against_store", raise_password_not_set)
    monkeypatch.setattr("palmimo_portal.api.auth.verify_password_against_store", raise_password_not_set)

    response = client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "auth_not_set"


def test_require_full_session_rejects_an_unrecognized_session_mode(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    from palmimo_portal.core.auth import issue_session

    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)
    auth = adapters.state.read_auth()
    assert auth is not None
    forged_token = issue_session(auth.signing_key, mode="some-future-mode")
    client.cookies.set(SESSION_COOKIE_NAME, forged_token)

    response = client.get("/api/v1/ssh-keys")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "initial_password_must_be_changed"


def test_change_password_locks_out_after_five_wrong_current_password_attempts(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # An attacker holding a stolen session cookie must not get an
    # unlimited online oracle to brute-force the current password.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    for _ in range(5):
        failed = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "wrong", "new_password": "new-password"},
            headers=CSRF_HEADERS,
        )
        assert failed.status_code == 401

    locked = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "hunter2", "new_password": "new-password"},
        headers=CSRF_HEADERS,
    )

    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "auth_rate_limited"


def test_change_password_from_full_with_no_current_password_is_401_and_does_not_consume_budget(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # A full session omitting current_password is a malformed request, not
    # a guess -- it must be rejected before try_attempt() so it cannot be
    # used to burn the shared login/change-password rate-limit budget.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    # One more than MAX_LOGIN_FAILURES: if any of these consumed budget,
    # the correct attempt below would be locked out.
    for _ in range(6):
        response = client.post(
            "/api/v1/auth/change-password",
            json={"new_password": "new-password"},
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "invalid_current_password"

    correct = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "hunter2", "new_password": "new-password"},
        headers=CSRF_HEADERS,
    )

    assert correct.status_code == 200


def test_change_password_and_login_share_the_same_rate_limit_budget(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    for _ in range(3):
        response = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "wrong", "new_password": "new-password"},
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 401
    for _ in range(2):
        failed = client.post("/api/v1/auth/login", json={"password": "wrong"}, headers=CSRF_HEADERS)
        assert failed.status_code == 401

    locked = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "hunter2", "new_password": "new-password"},
        headers=CSRF_HEADERS,
    )

    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "auth_rate_limited"


def test_change_password_lockout_check_runs_before_verification(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Once locked out, even the *correct* current_password must not be
    # verified -- the lockout check has to happen before verification.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    for _ in range(5):
        client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "wrong", "new_password": "new-password"},
            headers=CSRF_HEADERS,
        )

    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "hunter2", "new_password": "new-password"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 429
    # The password must not actually have been changed -- confirm the old
    # one still works once the lockout window has passed. We cannot fast
    # forward the shared LoginRateLimiter's real clock from an HTTP test,
    # so instead assert directly that auth.json's hash is unchanged.
    auth = adapters.state.read_auth()
    assert auth is not None
    from palmimo_portal.core.auth import verify_password

    assert verify_password("hunter2", auth.password_hash)


def test_change_password_from_full_maps_a_lock_timeout_to_409(
    client: TestClient, adapters: FakeAdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A concurrent full-mode change already holding StateStore.lock_auth()
    # past its own timeout must not surface as a bare 500 -- api/auth.py
    # translates AuthLockTimeoutError into a distinct, retryable envelope.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    @contextlib.contextmanager
    def timed_out_lock() -> Iterator[None]:
        raise AuthLockTimeoutError()
        yield  # pragma: no cover -- unreachable, satisfies the generator contract

    monkeypatch.setattr(adapters.state, "lock_auth", timed_out_lock)

    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "hunter2", "new_password": "new-password"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "auth_change_in_progress"


def test_change_password_lock_timeout_does_not_consume_rate_limit_budget(
    client: TestClient, adapters: FakeAdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A lock-acquisition timeout never actually verifies current_password
    # -- it must not eat into the same 5-attempt budget a real wrong
    # password would. Five timeouts in a row followed by the *correct*
    # current_password must still succeed.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    @contextlib.contextmanager
    def timed_out_lock() -> Iterator[None]:
        raise AuthLockTimeoutError()
        yield  # pragma: no cover -- unreachable, satisfies the generator contract

    monkeypatch.setattr(adapters.state, "lock_auth", timed_out_lock)
    for _ in range(5):
        response = client.post(
            "/api/v1/auth/change-password",
            json={"current_password": "hunter2", "new_password": "new-password"},
            headers=CSRF_HEADERS,
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "auth_change_in_progress"

    monkeypatch.undo()

    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "hunter2", "new_password": "new-password"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 200


def test_login_after_five_auth_not_set_responses_is_not_rate_limited(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Regression the reviewer reproduced: on a DIY device with no password
    # yet, /auth/login answers 409 auth_not_set without ever touching a
    # credential -- that must not spend the rate-limit budget. A correct
    # login right after /auth/setup must succeed, even after several
    # auth_not_set responses beforehand.
    _provision(adapters)
    for _ in range(5):
        response = client.post("/api/v1/auth/login", json={"password": "anything"}, headers=CSRF_HEADERS)
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "auth_not_set"

    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)

    response = client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert SESSION_COOKIE_NAME in response.cookies


def test_login_identity_unavailable_does_not_consume_rate_limit_budget(
    client: TestClient, adapters: FakeAdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 503 identity_unavailable is raised before try_attempt() is ever
    # called (auth-state resolution happens first) -- confirmed here from
    # the outside: several of them followed by a real setup+login must
    # still succeed normally, not 429.
    from palmimo_portal.ports import IDENTITY_UNAVAILABLE

    monkeypatch.setattr(adapters.identity, "read_identity", lambda: IDENTITY_UNAVAILABLE)
    for _ in range(5):
        response = client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "identity_unavailable"

    monkeypatch.undo()

    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _provision(adapters)
    response = client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 200
