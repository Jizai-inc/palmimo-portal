"""Tests for ``/api/v1/secrets`` and ``/api/v1/git-credentials``."""

from __future__ import annotations

from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.core.periodic import run_git_check_sweep
from palmimo_portal.ports import AppRecord, AppSource, AppsState, GitCommandError
from palmimo_portal.testing.fakes import FakeAdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


def _authenticated_client(client: TestClient, adapters: FakeAdapterBundle) -> TestClient:
    client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    adapters.network.known_networks.add("home")
    client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=CSRF_HEADERS)
    return client


def test_put_secret_then_list_never_exposes_the_value(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    put_response = client.put("/api/v1/secrets/OPENAI_API_KEY", json={"value": "sk-super-secret"}, headers=CSRF_HEADERS)
    assert put_response.status_code == 200
    assert "sk-super-secret" not in put_response.text

    list_response = client.get("/api/v1/secrets")
    assert list_response.status_code == 200
    assert list_response.json() == {
        "secrets": [{"name": "OPENAI_API_KEY", "updated_at": put_response.json()["updated_at"], "used_by": []}]
    }
    assert "sk-super-secret" not in list_response.text


def test_list_secrets_reports_used_by_apps_with_a_binding(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    client.put("/api/v1/secrets/OPENAI_API_KEY", json={"value": "sk-secret"}, headers=CSRF_HEADERS)
    adapters.secrets.write_bindings("palmimo-teleop", {"API_KEY": "OPENAI_API_KEY"})
    adapters.secrets.write_bindings("palmimo-companion", {"KEY": "OPENAI_API_KEY"})

    response = client.get("/api/v1/secrets")

    assert response.status_code == 200
    [secret] = response.json()["secrets"]
    assert secret["used_by"] == ["palmimo-companion", "palmimo-teleop"]


def test_list_secrets_reports_an_empty_used_by_for_an_unbound_secret(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    client.put("/api/v1/secrets/OPENAI_API_KEY", json={"value": "sk-secret"}, headers=CSRF_HEADERS)

    response = client.get("/api/v1/secrets")

    assert response.status_code == 200
    [secret] = response.json()["secrets"]
    assert secret["used_by"] == []


def test_put_secret_rejects_lowercase_name(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.put("/api/v1/secrets/lowercase", json={"value": "x"}, headers=CSRF_HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_secret_name"


def test_put_secret_rejects_value_with_newline(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.put("/api/v1/secrets/OPENAI_API_KEY", json={"value": "line1\nline2"}, headers=CSRF_HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_secret_value"


def test_delete_secret_not_found_returns_404(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.delete("/api/v1/secrets/MISSING", headers=CSRF_HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "secret_not_found"


def test_delete_secret_in_use_returns_409_with_users(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)
    client.put("/api/v1/secrets/OPENAI_API_KEY", json={"value": "sk-1"}, headers=CSRF_HEADERS)
    adapters.secrets.write_bindings("palmimo-teleop", {"API_KEY": "OPENAI_API_KEY"})

    response = client.delete("/api/v1/secrets/OPENAI_API_KEY", headers=CSRF_HEADERS)

    assert response.status_code == 409
    body = response.json()["error"]
    assert body["code"] == "secret_in_use"
    assert body["params"]["users"] == [{"app_id": "palmimo-teleop", "app_name": "palmimo-teleop", "request": "API_KEY"}]


def test_delete_secret_in_use_reports_the_apps_display_name_not_just_its_id(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # id and display name diverge whenever the manifest's `name` differs from the id's name
    # part (a suffix bump, or a rename design doc 3.9 accepts) -- users must be able to tell
    # which of their apps a locked secret belongs to, not just its opaque id.
    client = _authenticated_client(client, adapters)
    adapters.state.write_apps_state(
        AppsState(
            apps={
                "alice.teleop-2": AppRecord(
                    name="teleop",
                    source=AppSource(type="zip"),
                    installed_at=1.0,
                    params={},
                    autostart=False,
                    last_job=None,
                    id="alice.teleop-2",
                )
            }
        )
    )
    client.put("/api/v1/secrets/OPENAI_API_KEY", json={"value": "sk-1"}, headers=CSRF_HEADERS)
    adapters.secrets.write_bindings("alice.teleop-2", {"API_KEY": "OPENAI_API_KEY"})

    response = client.delete("/api/v1/secrets/OPENAI_API_KEY", headers=CSRF_HEADERS)

    assert response.json()["error"]["params"]["users"] == [
        {"app_id": "alice.teleop-2", "app_name": "teleop", "request": "API_KEY"}
    ]


def test_put_git_credential_never_exposes_token(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.put(
        "/api/v1/git-credentials/github.com%2FJizai-inc", json={"value": "ghp_secrettoken"}, headers=CSRF_HEADERS
    )

    assert response.status_code == 200
    assert "ghp_secrettoken" not in response.text
    assert adapters.secrets.get_git_credential("github.com/Jizai-inc") == "ghp_secrettoken"


def test_put_git_credential_normalizes_a_mixed_case_host_with_a_trailing_slash(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)

    response = client.put(
        "/api/v1/git-credentials/GitHub.com%2FJizai-inc/", json={"value": "ghp_token"}, headers=CSRF_HEADERS
    )

    assert response.status_code == 200
    assert response.json()["host_owner"] == "github.com/jizai-inc"
    assert adapters.secrets.get_git_credential("github.com/Jizai-inc") == "ghp_token"


def test_put_git_credential_with_no_owner_segment_returns_422(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.put("/api/v1/git-credentials/github.com", json={"value": "ghp_token"}, headers=CSRF_HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_host_owner"


def test_put_git_credential_clears_credential_rejected_on_apps_scoped_to_it(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    record = AppRecord(
        name="palmimo-teleop",
        source=AppSource(type="git", url="https://github.com/Jizai-inc/repo", ref="main", ref_kind="branch"),
        installed_at=1.0,
        params={},
        autostart=False,
        last_job=None,
        credential_rejected=True,
    )
    adapters.state.write_apps_state(AppsState(apps={"palmimo-teleop": record}))

    client.put("/api/v1/git-credentials/github.com%2FJizai-inc", json={"value": "ghp_freshtoken"}, headers=CSRF_HEADERS)

    assert adapters.state.read_apps_state().apps["palmimo-teleop"].credential_rejected is False


def test_list_git_credentials_shows_rejected_at_after_a_periodic_sweep_rejection(
    app: FastAPI, client: TestClient, adapters: FakeAdapterBundle
) -> None:
    client = _authenticated_client(client, adapters)
    client.put("/api/v1/git-credentials/github.com%2Facme", json={"value": "ghp_token"}, headers=CSRF_HEADERS)
    record = AppRecord(
        name="palmimo-teleop",
        source=AppSource(type="git", url="https://github.com/acme/repo", ref="main", ref_kind="branch"),
        installed_at=1.0,
        params={},
        autostart=False,
        last_job=None,
    )
    adapters.state.write_apps_state(AppsState(apps={"palmimo-teleop": record}))
    adapters.git.raise_on_fetch_commit_for[("https://github.com/acme/repo", "main", "branch")] = GitCommandError(
        "nope", status_code=401
    )

    run_git_check_sweep(app.state.apps_job_context, adapters.state)

    response = client.get("/api/v1/git-credentials")

    assert response.status_code == 200
    entry = next(c for c in response.json()["credentials"] if c["host_owner"] == "github.com/acme")
    assert entry["rejected_at"] is not None


def test_list_git_credentials_reports_the_apps_installed_from_its_host_owner(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Without this, an operator deleting/rotating a credential has no way to see which
    # installed apps depend on it before they act.
    client = _authenticated_client(client, adapters)
    client.put("/api/v1/git-credentials/github.com%2FJizai-inc", json={"value": "ghp_token"}, headers=CSRF_HEADERS)
    record = AppRecord(
        name="palmimo-teleop",
        source=AppSource(type="git", url="https://github.com/Jizai-inc/repo", ref="main", ref_kind="branch"),
        installed_at=1.0,
        params={},
        autostart=False,
        last_job=None,
        id="palmimo.teleop",
    )
    adapters.state.write_apps_state(AppsState(apps={"palmimo.teleop": record}))

    response = client.get("/api/v1/git-credentials")

    assert response.status_code == 200
    entry = next(c for c in response.json()["credentials"] if c["host_owner"] == "github.com/jizai-inc")
    assert entry["app_ids"] == ["palmimo.teleop"]


def test_put_git_credential_saves_the_token_when_the_apps_ledger_is_corrupt(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    # Writing clear_credential_rejected's result back over a corrupt apps.json would silently
    # replace it with a fresh empty ledger -- the credential must still be saved.
    client = _authenticated_client(client, adapters)
    adapters.state.apps_state_corrupt = True

    response = client.put(
        "/api/v1/git-credentials/github.com%2FJizai-inc", json={"value": "ghp_secrettoken"}, headers=CSRF_HEADERS
    )

    assert response.status_code == 200
    assert adapters.secrets.get_git_credential("github.com/Jizai-inc") == "ghp_secrettoken"
    assert adapters.state.apps_state_corrupt is True


def test_delete_git_credential_is_a_no_op_when_absent(client: TestClient, adapters: FakeAdapterBundle) -> None:
    client = _authenticated_client(client, adapters)

    response = client.delete("/api/v1/git-credentials/github.com%2FJizai-inc", headers=CSRF_HEADERS)

    assert response.status_code == 200
