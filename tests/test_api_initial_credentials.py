"""Tests for the per-device initial-credentials auth model.

Covers the identity-carrying-device flow: sticker login (``mode="initial"``)
-> forced password change -> normal (``mode="full"``) operation -- and its
interaction with the pre-existing DIY (``open_setup``) flow, which these
tests confirm stays unchanged.
"""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import cast

import httpx
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.adapters.identity import FileIdentityStore
from palmimo_portal.core.auth import SESSION_COOKIE_NAME, change_password_from_initial
from palmimo_portal.ports import AuthState, Identity
from palmimo_portal.testing.fakes import FakeAdapterBundle
from palmimo_portal.wiring import AdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}
STICKER_PASSWORD = "sticker-correct-horse"
DEVICE_ID = "palmimo-042"


def _carry_identity(adapters: FakeAdapterBundle, password: str = STICKER_PASSWORD) -> None:
    adapters.identity.identity = Identity(device_id=DEVICE_ID, initial_password=password)


def _initial_login(client: TestClient) -> None:
    response = client.post("/api/v1/auth/login", json={"password": STICKER_PASSWORD}, headers=CSRF_HEADERS)
    assert response.status_code == 200
    assert response.json()["mode"] == "initial"


def _post_json_with_lone_surrogate(client: TestClient, url: str, payload: dict[str, str]) -> httpx.Response:
    # See test_api_auth.py's twin helper: httpx's own json= encoding would
    # fail before the request is even sent, so this sends the literal
    # `\uXXXX` escape (ASCII on the wire) instead, letting the server-side
    # json.loads() decode it back into an in-memory surrogate.
    body = json.dumps(payload).encode("ascii")
    headers = {**CSRF_HEADERS, "Content-Type": "application/json"}
    return client.post(url, content=body, headers=headers)


def test_open_setup_setup_works_once_then_409(client: TestClient) -> None:
    first = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    second = client.post("/api/v1/auth/setup", json={"password": "other"}, headers=CSRF_HEADERS)

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "auth_already_set"


def test_setup_is_409_when_identity_present_even_though_no_password_is_set(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)

    response = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "initial_credentials_required"


def test_setup_is_409_when_identity_present_regardless_of_provisioning(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    adapters.network.known_networks.add("home")

    response = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "initial_credentials_required"


def test_login_with_the_initial_password_succeeds_with_mode_initial(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)

    response = client.post("/api/v1/auth/login", json={"password": STICKER_PASSWORD}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert response.json()["mode"] == "initial"
    assert SESSION_COOKIE_NAME in response.cookies


def test_login_with_the_initial_password_works_while_unprovisioned(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # The whole point of the identity flow: it must be reachable before
    # Wi-Fi is ever configured, since it is the only way to obtain the
    # session that eventually unlocks the Wi-Fi endpoints.
    _carry_identity(adapters)

    response = client.post("/api/v1/auth/login", json={"password": STICKER_PASSWORD}, headers=CSRF_HEADERS)

    assert response.status_code == 200


def test_login_with_the_wrong_initial_password_is_rejected(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)

    response = client.post("/api/v1/auth/login", json={"password": "wrong"}, headers=CSRF_HEADERS)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_with_the_wrong_initial_password_rate_limits(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)

    for _ in range(5):
        failed = client.post("/api/v1/auth/login", json={"password": "wrong"}, headers=CSRF_HEADERS)
        assert failed.status_code == 401

    locked = client.post("/api/v1/auth/login", json={"password": STICKER_PASSWORD}, headers=CSRF_HEADERS)

    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "auth_rate_limited"


def test_login_with_an_unencodable_initial_password_is_rejected_not_500(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # A lone UTF-16 surrogate is valid JSON (json.loads accepts it) but
    # cannot be UTF-8 encoded -- must be treated as simply the wrong
    # sticker password, not surface .encode()'s UnicodeEncodeError as a 500.
    _carry_identity(adapters)

    response = _post_json_with_lone_surrogate(client, "/api/v1/auth/login", {"password": "\ud800"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_with_an_unencodable_initial_password_rate_limits(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)

    for _ in range(5):
        failed = _post_json_with_lone_surrogate(client, "/api/v1/auth/login", {"password": "\ud800"})
        assert failed.status_code == 401

    locked = client.post("/api/v1/auth/login", json={"password": STICKER_PASSWORD}, headers=CSRF_HEADERS)

    assert locked.status_code == 429
    assert locked.json()["error"]["code"] == "auth_rate_limited"


def test_change_password_from_initial_ignores_a_supplied_current_password(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # The initial-mode path performs no current-password verification at
    # all; a caller sending one anyway must not be rejected for it.
    _carry_identity(adapters)
    _initial_login(client)

    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "whatever", "new_password": "new-owner-password"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 200


def test_ssh_keys_is_403_with_an_initial_session(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)
    adapters.network.known_networks.add("home")
    _initial_login(client)

    response = client.get("/api/v1/ssh-keys")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "initial_password_must_be_changed"


def test_system_status_wifi_endpoints_are_403_with_an_initial_session(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    _initial_login(client)

    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "initial_password_must_be_changed"


def test_wifi_connect_is_403_with_an_initial_session_even_while_unprovisioned(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    _initial_login(client)

    response = client.post("/api/v1/wifi/connect", json={"ssid": "home", "psk": "secret123"}, headers=CSRF_HEADERS)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "initial_password_must_be_changed"


def test_wifi_is_401_without_any_session_when_identity_present_even_while_unprovisioned(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Preserve today's unauthenticated-wifi-while-unprovisioned behavior
    # ONLY in open_setup (DIY) mode -- an identity-carrying device always
    # session-gates Wi-Fi.
    _carry_identity(adapters)

    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_system_reboot_is_403_with_an_initial_session(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)
    adapters.network.known_networks.add("home")
    _initial_login(client)

    response = client.post("/api/v1/system/reboot", headers=CSRF_HEADERS)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "initial_password_must_be_changed"


def test_change_password_is_reachable_with_an_initial_session(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)
    _initial_login(client)

    response = client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "new-owner-password"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 200


def test_logout_is_reachable_with_an_initial_session(client: TestClient, adapters: FakeAdapterBundle) -> None:
    _carry_identity(adapters)
    _initial_login(client)

    response = client.post("/api/v1/auth/logout", headers=CSRF_HEADERS)

    assert response.status_code == 200


def test_change_password_from_initial_succeeds_while_the_login_rate_limiter_is_locked_out(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Proves zero coupling with the login rate limiter: even fully locked
    # out on login failures, the initial-mode change-password path must
    # still succeed, since it never touches the limiter.
    _carry_identity(adapters)
    _initial_login(client)

    for _ in range(5):
        failed = client.post("/api/v1/auth/login", json={"password": "wrong"}, headers=CSRF_HEADERS)
        assert failed.status_code == 401
    locked = client.post("/api/v1/auth/login", json={"password": STICKER_PASSWORD}, headers=CSRF_HEADERS)
    assert locked.status_code == 429

    response = client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "new-owner-password"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 200

    # ...and the lockout must still stand afterwards: the path records no
    # success either (a record_success() side effect would clear it).
    still_locked = client.post("/api/v1/auth/login", json={"password": "new-owner-password"}, headers=CSRF_HEADERS)
    assert still_locked.status_code == 429


def test_change_password_from_initial_success_moves_auth_state_to_set(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    _initial_login(client)

    client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "new-owner-password"},
        headers=CSRF_HEADERS,
    )

    status = client.get("/api/v1/system/status")
    assert status.json()["auth_state"] == "set"


def test_change_password_from_initial_invalidates_the_old_initial_session(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    _initial_login(client)
    old_cookie = client.cookies.get(SESSION_COOKIE_NAME)
    assert old_cookie is not None

    client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "new-owner-password"},
        headers=CSRF_HEADERS,
    )

    stale_client = TestClient(client.app, cookies={SESSION_COOKIE_NAME: old_cookie})
    stale_client.headers["Host"] = "testserver"
    response = stale_client.post("/api/v1/auth/logout", headers=CSRF_HEADERS)

    assert response.status_code == 401


def test_login_with_the_initial_password_fails_after_the_password_is_changed(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    _initial_login(client)
    client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "new-owner-password"},
        headers=CSRF_HEADERS,
    )
    client.cookies.delete(SESSION_COOKIE_NAME)

    response = client.post("/api/v1/auth/login", json={"password": STICKER_PASSWORD}, headers=CSRF_HEADERS)

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_credentials"


def test_login_with_the_new_password_works_after_the_password_is_changed(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    _initial_login(client)
    client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "new-owner-password"},
        headers=CSRF_HEADERS,
    )
    client.cookies.delete(SESSION_COOKIE_NAME)

    response = client.post("/api/v1/auth/login", json={"password": "new-owner-password"}, headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert response.json()["mode"] == "full"


def test_wifi_reachable_after_change_password_from_initial_using_the_reissued_session(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # change-password re-issues a full session in the same response, so the
    # caller does not need a second /login round trip to reach Wi-Fi --
    # necessary because, while unprovisioned, /login on an identity device
    # is the *only* other way to get a session, and getting one requires
    # already being provisioned in the open_setup sense... except this is
    # exactly what change-password's re-issued cookie sidesteps.
    _carry_identity(adapters)
    _initial_login(client)

    client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "new-owner-password"},
        headers=CSRF_HEADERS,
    )

    response = client.get("/api/v1/wifi/status")

    assert response.status_code == 200


def test_change_password_from_initial_concurrent_race_loser_gets_409(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Simulates a second initial session winning the create_auth race
    # *during* this request's own handling -- late enough that
    # SessionMiddleware has already accepted this request's (still-valid
    # at that point) initial cookie, but before this request's own
    # create_auth call runs. A "winner" that completed and rotated the key
    # before this request even started would instead show up as 401 from
    # require_auth (a separate, also-correct outcome -- proven by
    # test_change_password_is_401_once_auth_corrupts_after_an_initial_session_was_issued's
    # sibling case), which is not what this test is about.
    _carry_identity(adapters)
    _initial_login(client)

    original_create_auth = adapters.state.create_auth

    def racing_create_auth(state: AuthState) -> None:
        adapters.state.create_auth = original_create_auth  # type: ignore[method-assign]  # deliberate monkeypatch of a bound method for this test
        change_password_from_initial(adapters.state, "other-winner-password")
        original_create_auth(state)

    adapters.state.create_auth = racing_create_auth  # type: ignore[method-assign]  # deliberate monkeypatch of a bound method for this test

    response = client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "new-owner-password"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "auth_change_conflict"


def test_change_password_from_full_requires_the_correct_current_password(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "wrong", "new_password": "new-password"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_current_password"


def test_change_password_from_full_rotates_the_key_invalidating_the_old_session(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    old_cookie = client.cookies.get(SESSION_COOKIE_NAME)
    assert old_cookie is not None

    change_response = client.post(
        "/api/v1/auth/change-password",
        json={"current_password": "hunter2", "new_password": "new-password"},
        headers=CSRF_HEADERS,
    )
    assert change_response.status_code == 200

    stale_client = TestClient(client.app, cookies={SESSION_COOKIE_NAME: old_cookie})
    stale_client.headers["Host"] = "testserver"
    response = stale_client.get("/api/v1/ssh-keys")

    assert response.status_code == 401


def test_login_is_409_when_auth_corrupt_even_with_identity_present(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    _carry_identity(adapters)
    adapters.state.auth_corrupt = True

    response = client.post("/api/v1/auth/login", json={"password": STICKER_PASSWORD}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "auth_state_corrupt"


def test_change_password_is_401_once_auth_corrupts_after_an_initial_session_was_issued(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # The session was validly issued, but auth.json corrupting afterward
    # (e.g. a crash mid-write) must invalidate it just like every other
    # session -- change-password is not a backdoor around that.
    _carry_identity(adapters)
    _initial_login(client)

    adapters.state.auth_corrupt = True

    response = client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "new-owner-password"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "not_authenticated"


def test_setup_is_503_when_identity_is_unavailable(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # A transient identity-read failure (e.g. /boot/firmware not mounted
    # yet) must never be treated as "no identity file" -- that would let
    # anyone claim a sticker/OEM device through the unauthenticated DIY
    # setup flow for as long as the read keeps failing.
    adapters.identity.unavailable = True

    response = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_unavailable"


def test_login_is_503_when_identity_is_unavailable(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.identity.unavailable = True

    response = client.post("/api/v1/auth/login", json={"password": "anything"}, headers=CSRF_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_unavailable"


def test_login_is_503_not_409_or_open_setup_when_unprovisioned_and_identity_is_unavailable(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Locks the end-to-end consequence of require_provisioned_unless_identity
    # treating IDENTITY_UNAVAILABLE the same as "an identity file is
    # present" (see its docstring/inline comment): on a device that is both
    # unprovisioned (no known networks) *and* has a transiently-unreadable
    # identity file, an unauthenticated login attempt must reach login's own
    # compute_auth_state() check and refuse with 503 identity_unavailable --
    # never 409 not_provisioned (which would mean the gate fell through to
    # the "no identity file" branch) and never a 200/401 as open_setup would
    # produce (which would mean the unavailable read got silently treated as
    # clean absence, misrouting a sticker/OEM device into the DIY flow).
    assert not adapters.network.known_networks  # unprovisioned
    adapters.identity.unavailable = True

    response = client.post("/api/v1/auth/login", json={"password": "anything"}, headers=CSRF_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_unavailable"


def test_change_password_from_initial_is_503_when_identity_is_unavailable(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # SessionMiddleware itself re-reads identity_store.read_identity() on
    # every request to verify an initial-mode cookie, so an identity that is
    # *already* unavailable by the time this request arrives would fail
    # authentication at 401 before ever reaching the handler's own
    # identity_unavailable check (api/auth.py L297-302) -- this test instead
    # simulates the narrower race that check actually guards: the identity
    # read is healthy for SessionMiddleware's own check earlier in the same
    # request, but flips to unavailable by the time the handler makes its
    # own second read (a transient failure landing squarely inside one
    # request's handling).
    _carry_identity(adapters)
    _initial_login(client)
    real_read_identity = adapters.identity.read_identity
    call_count = 0

    def flaky_read_identity() -> object:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return real_read_identity()  # SessionMiddleware's own check -- must still authenticate
        adapters.identity.unavailable = True
        return real_read_identity()  # the handler's own re-read -- now unavailable

    adapters.identity.read_identity = flaky_read_identity  # type: ignore[method-assign,assignment]  # deliberate monkeypatch of a bound method for this test

    response = client.post(
        "/api/v1/auth/change-password",
        json={"new_password": "new-password"},
        headers=CSRF_HEADERS,
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "identity_unavailable"


def test_login_still_works_when_a_password_is_already_set_even_if_identity_is_unavailable(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Once auth.json is PRESENT, an owner already exists and login checks
    # the stored hash directly -- the identity file is irrelevant at that
    # point, so a transient failure reading it must not turn into a DoS
    # against a device that already has a real password. auth_file_state
    # PRESENT takes priority over an unavailable identity read (see
    # compute_auth_state / test_set_takes_priority_over_unavailable).
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    adapters.identity.unavailable = True

    response = client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    assert response.status_code == 200


def test_status_reports_auth_state_unavailable(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.identity.unavailable = True

    response = client.get("/api/v1/system/status")

    assert response.status_code == 200
    assert response.json()["auth_state"] == "unavailable"
    assert response.json()["device_id"] is None


def test_status_does_not_crash_when_identity_is_unavailable_and_auth_present(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # A password is already set (device_id reporting only ever depended on
    # a *successful* identity parse) -- unavailable must not be conflated
    # with a real Identity when computing device_id.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.identity.unavailable = True

    response = client.get("/api/v1/system/status")

    assert response.status_code == 200
    assert response.json()["auth_state"] == "set"
    assert response.json()["device_id"] is None


def test_identity_becoming_available_again_re_reads_correctly(client: TestClient, adapters: FakeAdapterBundle) -> None:
    # Once the transient failure clears (e.g. the mount finishes), the
    # device must correctly resume as open_setup or initial -- not stay
    # stuck, since F1 requires the unavailable read to never be cached.
    adapters.identity.unavailable = True
    unavailable_response = client.get("/api/v1/system/status")
    assert unavailable_response.json()["auth_state"] == "unavailable"

    adapters.identity.unavailable = False

    response = client.get("/api/v1/system/status")

    assert response.json()["auth_state"] == "open_setup"


#
# The malformed-file -> None + "log ERROR once" contract is unit-tested
# directly against the real adapter in test_identity_adapter.py
# (test_read_identity_treats_malformed_json_as_absent and
# test_read_identity_logs_the_error_exactly_once): FileIdentityStore
# already collapses a malformed file to None before anything in api/ or
# deps.py ever sees it, so from the API's point of view a malformed file
# and a genuinely absent one are indistinguishable -- covered here by
# test_open_setup_setup_works_once_then_409 and
# test_status_reports_auth_state_open_setup_before_setup, both of which
# exercise the default (no identity) FakeIdentityStore.


def test_a_v1_format_identity_file_is_treated_as_open_setup(
    tmp_path: Path, app: FastAPI, adapters: FakeAdapterBundle, client: TestClient
) -> None:
    # A spec-v1 identity file (initial_password_hash, not v2's
    # initial_password) is malformed under the current contract, so a
    # device migrated to v2 firmware but still carrying a v1 identity file
    # must fall back to open_setup -- exactly like a genuinely absent file --
    # rather than being bricked or silently accepting the old hash as a
    # plaintext password.
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(json.dumps({"device_id": DEVICE_ID, "initial_password_hash": "argon2id$..."}))
    identity_store = FileIdentityStore(identity_path)
    app.state.adapters = dataclasses.replace(cast(AdapterBundle, adapters), identity=identity_store)

    status = client.get("/api/v1/system/status")
    assert status.json()["auth_state"] == "open_setup"
    assert status.json()["device_id"] is None

    setup = client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    assert setup.status_code == 200

    login = client.post("/api/v1/auth/login", json={"password": STICKER_PASSWORD}, headers=CSRF_HEADERS)
    assert login.status_code == 401
    assert login.json()["error"]["code"] == "invalid_credentials"
