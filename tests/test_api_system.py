"""Tests for ``/api/v1/system``."""

from __future__ import annotations

import dataclasses
import json
import socket
from pathlib import Path
from typing import cast

from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.adapters.identity import FileIdentityStore
from palmimo_portal.ports import AdapterUnavailableError, UpdateJob, UpdateState
from palmimo_portal.testing.fakes import FakeAdapterBundle
from palmimo_portal.wiring import AdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


def test_status_is_reachable_without_a_session(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")

    assert response.status_code == 200


def test_status_reports_the_machine_hostname(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")

    assert response.json()["hostname"] == socket.gethostname()


def test_status_reports_auth_state_open_setup_before_setup(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")

    assert response.json()["auth_state"] == "open_setup"


def test_status_reports_auth_state_set_after_setup(client: TestClient) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    response = client.get("/api/v1/system/status")

    assert response.json()["auth_state"] == "set"


def test_status_reports_auth_state_corrupt(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.state.auth_corrupt = True

    response = client.get("/api/v1/system/status")

    assert response.json()["auth_state"] == "corrupt"


def test_status_reports_auth_state_initial_when_identity_present_and_no_password_set(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    from palmimo_portal.core.auth import hash_password
    from palmimo_portal.ports import Identity

    adapters.identity.identity = Identity(device_id="palmimo-042", initial_password_hash=hash_password("sticker"))

    response = client.get("/api/v1/system/status")

    assert response.json()["auth_state"] == "initial"


def test_status_reports_auth_state_set_when_identity_present_and_password_already_set(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    from palmimo_portal.core.auth import hash_password
    from palmimo_portal.ports import Identity

    adapters.identity.identity = Identity(device_id="palmimo-042", initial_password_hash=hash_password("sticker"))
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    response = client.get("/api/v1/system/status")

    # /setup is 409 when identity is present, so the password never got
    # set -- confirming the request above failed is part of what this
    # test proves; the state must therefore still be "initial", not "set".
    assert response.json()["auth_state"] == "initial"


def test_status_device_id_is_null_without_an_identity_file(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")

    assert response.json()["device_id"] is None


def test_status_reports_the_device_id_when_an_identity_file_is_present(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    from palmimo_portal.core.auth import hash_password
    from palmimo_portal.ports import Identity

    adapters.identity.identity = Identity(device_id="palmimo-042", initial_password_hash=hash_password("sticker"))

    response = client.get("/api/v1/system/status")

    assert response.json()["device_id"] == "palmimo-042"


def test_status_reflects_an_identity_file_removed_from_disk_without_a_restart(
    tmp_path: Path, app: FastAPI, adapters: FakeAdapterBundle, client: TestClient
) -> None:
    # /system/status is the endpoint an operator watches while diagnosing a
    # device -- it must read the real (uncached) identity store, not a
    # cache primed from an earlier request, so a removed identity file
    # shows up immediately rather than only after a process restart.
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "argon2id$..."}))
    identity_store = FileIdentityStore(identity_path)
    # A deliberate mixed bundle -- a real adapter standing in for the
    # identity port only, everything else stays fake -- so cast to the
    # general AdapterBundle (see FakeAdapterBundle's docstring) for the
    # replace() call itself.
    app.state.adapters = dataclasses.replace(cast(AdapterBundle, adapters), identity=identity_store)

    first = client.get("/api/v1/system/status")
    assert first.json()["device_id"] == "palmimo-042"

    identity_path.unlink()

    second = client.get("/api/v1/system/status")
    assert second.json()["device_id"] is None


def test_status_reports_the_portal_package_version(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")

    assert response.json()["versions"]["portal"] != ""


def test_status_reports_the_active_adapter_mode(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")

    assert response.json()["adapters"] == "fake"


def test_status_reports_the_state_dir(client: TestClient, app: FastAPI) -> None:
    response = client.get("/api/v1/system/status")

    assert response.json()["state_dir"] == str(app.state.settings.state_dir)


def test_status_last_wifi_attempt_is_null_before_any_attempt(client: TestClient) -> None:
    response = client.get("/api/v1/system/status")

    assert response.json()["last_wifi_attempt"] is None


def test_reboot_calls_the_system_port(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    response = client.post("/api/v1/system/reboot", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert adapters.system.reboot_calls == 1


def test_shutdown_calls_the_system_port(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)

    response = client.post("/api/v1/system/shutdown", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert adapters.system.shutdown_calls == 1


def _write_running_update_job(adapters: FakeAdapterBundle) -> None:
    adapters.state.write_update_state(
        UpdateState(
            latest=None,
            checked_at=None,
            previous_tag=None,
            job=UpdateJob(
                state="running",
                kind="update",
                target="v2.0.0",
                step="sync",
                error=None,
                started_at=1.0,
                finished_at=None,
            ),
        )
    )


def test_reboot_is_409_while_the_update_runner_is_alive(
    client: TestClient, adapters: FakeAdapterBundle, app: FastAPI
) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _write_running_update_job(adapters)
    app.state.update_runner_alive.set()

    response = client.post("/api/v1/system/reboot", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "update_in_progress"
    assert adapters.system.reboot_calls == 0


def test_shutdown_is_409_while_the_update_runner_is_alive(
    client: TestClient, adapters: FakeAdapterBundle, app: FastAPI
) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _write_running_update_job(adapters)
    app.state.update_runner_alive.set()

    response = client.post("/api/v1/system/shutdown", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "update_in_progress"
    assert adapters.system.shutdown_calls == 0


def test_reboot_self_heals_and_succeeds_when_a_running_job_has_no_live_runner(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # A "running" job with no live runner behind it in this process can
    # only be a dead thread that failed to persist its own failure -- it
    # must not block reboot forever, the same self-healing
    # GET /update/status already does.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    _write_running_update_job(adapters)

    response = client.post("/api/v1/system/reboot", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert adapters.system.reboot_calls == 1
    assert adapters.state.read_update_state().job.state == "failed"


def test_reboot_is_allowed_while_an_update_job_is_only_restarting(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # "restarting" means the update has already fully applied and is only
    # waiting on the restart itself -- rebooting here does not corrupt
    # anything, and gives the operator a manual way to finish a restart
    # that is not landing on its own.
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.state.write_update_state(
        UpdateState(
            latest=None,
            checked_at=None,
            previous_tag=None,
            job=UpdateJob(
                state="restarting",
                kind="update",
                target="v2.0.0",
                step="checkout",
                error=None,
                started_at=1.0,
                finished_at=None,
                restarting_at=1.5,
            ),
        )
    )

    response = client.post("/api/v1/system/reboot", headers=CSRF_HEADERS)

    assert response.status_code == 200
    assert adapters.system.reboot_calls == 1


def test_status_maps_adapter_unavailable_to_a_503_envelope(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.network.raise_on_get_status = AdapterUnavailableError("network_backend_unavailable", "comitup down")

    response = client.get("/api/v1/system/status")

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "network_backend_unavailable"


def test_reboot_maps_adapter_unavailable_to_a_503_envelope(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.system.raise_on_reboot = AdapterUnavailableError("system_backend_unavailable", "logind down")

    response = client.post("/api/v1/system/reboot", headers=CSRF_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "system_backend_unavailable"


def test_shutdown_maps_adapter_unavailable_to_a_503_envelope(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.system.raise_on_shutdown = AdapterUnavailableError("system_backend_unavailable", "logind down")

    response = client.post("/api/v1/system/shutdown", headers=CSRF_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "system_backend_unavailable"
