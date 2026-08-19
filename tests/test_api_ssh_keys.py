"""Tests for ``/api/v1/ssh-keys``."""

from __future__ import annotations

import base64
import dataclasses
import os
import stat
import struct
from pathlib import Path
from typing import cast
from urllib.parse import quote

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.adapters.ssh_keys import AuthorizedKeysSshKeyPort
from palmimo_portal.ports import SshKey, SshKeysLockTimeoutError
from palmimo_portal.testing.fakes import FakeAdapterBundle
from palmimo_portal.wiring import AdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


def _key(material: bytes, comment: str = "user@laptop", key_type: str = "ssh-ed25519") -> str:
    type_bytes = key_type.encode("ascii")
    wire = struct.pack(">I", len(type_bytes)) + type_bytes + material
    blob = base64.b64encode(wire).decode("ascii")
    return f"{key_type} {blob} {comment}"


KEY_A = _key(b"api-test-key-a-00000000000")
KEY_B = _key(b"api-test-key-b-00000000000", comment="second@device")


def _authenticated_client(client: TestClient, adapters: FakeAdapterBundle) -> TestClient:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    return client


def test_add_then_list_returns_the_key(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    add_response = client.post("/api/v1/ssh-keys", json={"public_key": KEY_A}, headers=CSRF_HEADERS)
    assert add_response.status_code == 201

    list_response = client.get("/api/v1/ssh-keys")
    assert list_response.status_code == 200
    [key] = list_response.json()
    assert key["fingerprint"] == add_response.json()["fingerprint"]
    assert key["key_type"] == "ssh-ed25519"


def test_add_rejects_an_invalid_key_format(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.post("/api/v1/ssh-keys", json={"public_key": "garbage"}, headers=CSRF_HEADERS)

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_key_format"


def test_add_rejects_a_blob_that_is_valid_base64_but_not_a_real_key(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    garbage_blob = base64.b64encode(b"not-a-real-ssh-key-blob").decode("ascii")

    response = client.post(
        "/api/v1/ssh-keys", json={"public_key": f"ssh-ed25519 {garbage_blob} comment"}, headers=CSRF_HEADERS
    )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_key_format"


def test_add_rejects_a_duplicate_key(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    client.post("/api/v1/ssh-keys", json={"public_key": KEY_A}, headers=CSRF_HEADERS)

    response = client.post("/api/v1/ssh-keys", json={"public_key": KEY_A}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "duplicate_key"


def test_delete_the_last_key_without_confirmation_is_rejected(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    added = client.post("/api/v1/ssh-keys", json={"public_key": KEY_A}, headers=CSRF_HEADERS).json()

    response = client.delete(f"/api/v1/ssh-keys/{quote(added['fingerprint'], safe='')}", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_key_deletion_requires_confirmation"
    assert client.get("/api/v1/ssh-keys").json() != []


def test_delete_the_last_key_with_confirmation_succeeds(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    added = client.post("/api/v1/ssh-keys", json={"public_key": KEY_A}, headers=CSRF_HEADERS).json()

    response = client.delete(
        f"/api/v1/ssh-keys/{quote(added['fingerprint'], safe='')}", params={"confirm": "last-key"}, headers=CSRF_HEADERS
    )

    assert response.status_code == 200
    assert client.get("/api/v1/ssh-keys").json() == []


def test_delete_a_key_that_is_not_the_last_needs_no_confirmation(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    added_a = client.post("/api/v1/ssh-keys", json={"public_key": KEY_A}, headers=CSRF_HEADERS).json()
    client.post("/api/v1/ssh-keys", json={"public_key": KEY_B}, headers=CSRF_HEADERS)

    response = client.delete(f"/api/v1/ssh-keys/{quote(added_a['fingerprint'], safe='')}", headers=CSRF_HEADERS)

    assert response.status_code == 200
    [remaining] = client.get("/api/v1/ssh-keys").json()
    assert remaining["comment"] == "second@device"


def test_delete_an_unknown_fingerprint_is_404(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.delete("/api/v1/ssh-keys/SHA256:doesnotexist", headers=CSRF_HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "key_not_found"


class _LockTimeoutSshKeyPort:
    """Stands in for a real adapter whose ``authorized_keys`` lock is contended past its bound."""

    def list_keys(self) -> list[SshKey]:
        return []

    def add_key(self, public_key: str) -> SshKey:
        raise SshKeysLockTimeoutError()

    def delete_key(self, fingerprint: str, *, allow_last: bool = False) -> None:
        raise SshKeysLockTimeoutError()


def test_add_key_maps_lock_timeout_to_409_ssh_keys_busy(
    app: FastAPI, adapters: FakeAdapterBundle, client: TestClient
) -> None:
    app.state.adapters = dataclasses.replace(cast(AdapterBundle, adapters), ssh_keys=_LockTimeoutSshKeyPort())
    client = _authenticated_client(client, adapters)

    response = client.post("/api/v1/ssh-keys", json={"public_key": KEY_A}, headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ssh_keys_busy"


def test_delete_key_maps_lock_timeout_to_409_ssh_keys_busy(
    app: FastAPI, adapters: FakeAdapterBundle, client: TestClient
) -> None:
    app.state.adapters = dataclasses.replace(cast(AdapterBundle, adapters), ssh_keys=_LockTimeoutSshKeyPort())
    client = _authenticated_client(client, adapters)

    response = client.delete("/api/v1/ssh-keys/SHA256:doesnotexist", headers=CSRF_HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "ssh_keys_busy"


def test_add_key_returns_500_and_leaves_authorized_keys_unchanged_when_unwritable(
    tmp_path: Path, app: FastAPI, adapters: FakeAdapterBundle, client: TestClient
) -> None:
    # The real adapter's atomic_write_text (temp-then-rename) means an
    # unwritable target directory fails before any partial file is ever
    # created -- api/ssh_keys.py has no OSError handling of its own, so
    # this reaches the app-wide 500 internal_error handler, and the
    # existing file (if any) must be left exactly as it was.
    authorized_keys = tmp_path / "ssh" / "authorized_keys"
    authorized_keys.parent.mkdir(parents=True)
    authorized_keys.write_text(f"{KEY_A}\n", encoding="utf-8")
    os.chmod(authorized_keys.parent, stat.S_IRUSR | stat.S_IXUSR)  # r-x: no write into this directory
    real_ssh_keys = AuthorizedKeysSshKeyPort(path=authorized_keys)
    app.state.adapters = dataclasses.replace(cast(AdapterBundle, adapters), ssh_keys=real_ssh_keys)
    client = _authenticated_client(client, adapters)

    try:
        # Starlette's ServerErrorMiddleware re-raises after sending the 500
        # response it builds from the registered Exception handler -- the
        # TestClient (raise_server_exceptions=True, the default) surfaces
        # that re-raise directly rather than swallowing it (same pattern as
        # test_api_reset.py's test_a_failing_delete_auth_does_not_burn_...).
        with pytest.raises(PermissionError):
            client.post("/api/v1/ssh-keys", json={"public_key": KEY_B}, headers=CSRF_HEADERS)
    finally:
        os.chmod(authorized_keys.parent, stat.S_IRWXU)  # restore so tmp_path cleanup can remove it

    assert authorized_keys.read_text(encoding="utf-8") == f"{KEY_A}\n"
