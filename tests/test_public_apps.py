from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.adapters.public_catalog import parse_public_catalog
from palmimo_portal.core.periodic import run_git_check_sweep
from palmimo_portal.ports import (
    GitCommandError,
    PublicCatalogApp,
    PublicCatalogAsset,
    PublicCatalogSourceError,
    UnitStatus,
)
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


HEADERS = {"X-Requested-With": "PalmimoPortal"}
APP_ID = "alice.dance"


@pytest.fixture
def public_app() -> PublicCatalogApp:
    return parse_public_catalog(
        (Path(__file__).parent / "fixtures/public_catalog/catalog.example.json").read_bytes()
    ).apps[0]


@pytest.fixture
def app(tmp_path: Path) -> FastAPI:
    from palmimo_portal.api.app import create_app

    platform = tmp_path / "platform"
    (platform / "current").mkdir(parents=True)
    (platform / "current/install.sh").write_text("#!/bin/sh\n")
    return create_app(
        Settings(
            allowed_hosts=frozenset({"testserver"}),
            apps_dir=tmp_path / "apps",
            uv_cache_dir=tmp_path / "uv-cache",
            platform_dir=platform,
            static_dir=tmp_path / "static",
            apps_run_in_thread=False,
            periodic_enabled=False,
        )
    )


@pytest.fixture
def adapters(app: FastAPI, public_app: PublicCatalogApp) -> FakeAdapterBundle:
    bundle: FakeAdapterBundle = app.state.adapters
    bundle.public_catalog.asset = PublicCatalogAsset("2026-11-09T00:00:00Z", (public_app,))

    def seed(dest: Path, url: str, ref: str, kind: str) -> None:
        project = dest / (public_app.source.subdir or "")
        project.mkdir(parents=True)
        (project / (public_app.source.manifest or "palmimo.toml")).write_text(
            'schema = 1\nname = "display-name"\ndescription = "d"\ncommand = ["run"]\n'
            + f"devices = {json.dumps(list(public_app.devices))}\n"
            + "".join(f'[env.{name}]\nrequired = true\ndescription = "key"\n' for name in public_app.env)
        )
        (project / "pyproject.toml").write_text('[project]\nname="app"\nversion="0"\n')
        (project / ".venv/bin").mkdir(parents=True)
        (project / ".venv/bin/python").touch()

    bundle.git.on_clone = seed
    return bundle


@pytest.fixture
def client(app: FastAPI, adapters: FakeAdapterBundle) -> Iterator[TestClient]:
    with TestClient(app) as client:
        client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=HEADERS)
        adapters.saved_networks.save("home", "", first=False)
        client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=HEADERS)
        yield client


def install(client: TestClient, public_app: PublicCatalogApp) -> None:
    response = client.post(
        "/api/v1/apps/install-public",
        json={"public_app_id": public_app.public.app_id, "commit": public_app.source.commit},
        headers=HEADERS,
    )
    assert response.status_code == 202, response.text
    assert response.json()["job"]["state"] == "done", response.text


def refresh(app: FastAPI, adapters: FakeAdapterBundle, *apps: PublicCatalogApp) -> None:
    adapters.public_catalog.asset = PublicCatalogAsset("2026-11-09T01:00:00Z", tuple(apps))
    app.state.public_catalog_cache.refresh(ntp_synchronized=True)


def test_public_catalog_endpoint_fetches_on_first_request(
    client: TestClient, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    response = client.get("/api/v1/catalog/public")
    assert response.status_code == 200
    body = response.json()
    assert body["apps"][0]["public"]["app_id"] == public_app.public.app_id
    assert adapters.public_catalog.asset is not None
    assert body["generated_at"] == adapters.public_catalog.asset.generated_at
    assert body["fetched_at"] is not None
    assert body["stale"] is False
    assert adapters.public_catalog.fetch_calls == 1


def test_public_catalog_endpoint_reports_stale_success_after_failure(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle
) -> None:
    before = client.get("/api/v1/catalog/public").json()
    adapters.public_catalog.raise_on_fetch = PublicCatalogSourceError("offline")
    app.state.public_catalog_cache.refresh(ntp_synchronized=True)
    after = client.get("/api/v1/catalog/public").json()
    assert after["apps"] == before["apps"]
    assert after["fetched_at"] == before["fetched_at"]
    assert after["stale"] is True
    assert after["reason"] == "offline"


@pytest.mark.parametrize(
    "failure, status, code",
    [
        ("unavailable", 503, "public_catalog_unavailable"),
        ("missing", 404, "public_app_not_found"),
        ("changed", 409, "public_app_changed"),
    ],
)
def test_install_public_rejects_unavailable_or_changed_selection(
    client: TestClient,
    app: FastAPI,
    adapters: FakeAdapterBundle,
    public_app: PublicCatalogApp,
    failure: str,
    status: int,
    code: str,
) -> None:
    if failure == "unavailable":
        adapters.public_catalog.raise_on_fetch = PublicCatalogSourceError("offline")
    elif failure == "missing":
        refresh(app, adapters)
    response = client.post(
        "/api/v1/apps/install-public",
        json={
            "public_app_id": public_app.public.app_id,
            "commit": "f" * 40 if failure == "changed" else public_app.source.commit,
        },
        headers=HEADERS,
    )
    assert response.status_code == status
    assert response.json()["error"]["code"] == code
    assert adapters.state.read_apps_state().apps == {}
    assert adapters.git.clone_commit_calls == []


def test_install_public_uses_catalog_identity_and_commit(
    client: TestClient, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    install(client, public_app)
    record = adapters.state.read_apps_state().apps[APP_ID]
    assert record.name == "display-name"
    assert record.source == replace(public_app.source, public_app_id=public_app.public.app_id)
    assert adapters.git.clone_commit_calls == [(public_app.source.url, public_app.source.commit)]
    assert adapters.git.clone_calls == []


@pytest.mark.parametrize(
    "condition, available, update",
    [("same", True, False), ("new", True, True), ("missing", False, False), ("withdrawn", True, True)],
)
def test_public_update_status_is_derived_without_ledger_writes(
    client: TestClient,
    app: FastAPI,
    adapters: FakeAdapterBundle,
    public_app: PublicCatalogApp,
    condition: str,
    available: bool,
    update: bool,
) -> None:
    install(client, public_app)
    target = replace(public_app, source=replace(public_app.source, commit="a" * 40 if condition == "new" else "1" * 40))
    if condition == "same":
        target = public_app
    refresh(app, adapters, *(() if condition == "missing" else (target,)))
    before = adapters.state.read_apps_state()
    responses = [
        client.get("/api/v1/apps").json()["apps"][0],
        client.get(f"/api/v1/apps/{APP_ID}").json(),
        client.post(f"/api/v1/apps/{APP_ID}/update/check", headers=HEADERS).json(),
    ]
    for body in responses:
        assert body["public_app_available"] is available
        assert body["update_available"] is update
        key = "remote_commit" if "remote_commit" in body else "latest_commit"
        assert body[key] == (target.source.commit if available else None)
    assert adapters.state.read_apps_state() == before
    assert adapters.git.fetch_commit_calls == []


def test_public_update_uses_latest_source_and_preserves_local_id(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    install(client, public_app)
    target = replace(
        public_app,
        source=replace(
            public_app.source,
            url="https://github.com/carol/renamed",
            ref="main",
            ref_kind="branch",
            commit="b" * 40,
            subdir="relocated",
            manifest="palmimo.other.toml",
        ),
    )
    seed_original = adapters.git.on_clone
    assert seed_original is not None

    def seed_relocated(dest: Path, url: str, ref: str, kind: str) -> None:
        seed_original(dest, url, ref, kind)
        original = dest / (public_app.source.subdir or "")
        relocated = dest / "relocated"
        original.rename(relocated)
        (relocated / "palmimo.toml").rename(relocated / "palmimo.other.toml")

    adapters.git.on_clone = seed_relocated
    refresh(app, adapters, target)
    response = client.post(f"/api/v1/apps/{APP_ID}/update", json={"commit": target.source.commit}, headers=HEADERS)
    assert response.status_code == 202
    assert response.json()["job"]["state"] == "done", response.text
    record = adapters.state.read_apps_state().apps[APP_ID]
    assert record.id == APP_ID
    assert record.source == replace(target.source, public_app_id=public_app.public.app_id)
    assert adapters.git.clone_commit_calls[-1] == (target.source.url, target.source.commit)
    assert adapters.git.clone_calls == []


def test_public_update_unavailable_leaves_installed_app_untouched(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    install(client, public_app)
    before = adapters.state.read_apps_state()
    refresh(app, adapters)
    response = client.post(f"/api/v1/apps/{APP_ID}/update", headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "public_app_unavailable"
    assert adapters.state.read_apps_state() == before


@pytest.mark.parametrize("ref", ["v0.2.0", "next"])
def test_source_change_opts_out_of_public_updates_even_for_same_ref(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp, ref: str
) -> None:
    public_app = replace(public_app, source=replace(public_app.source, ref_kind="branch"))
    refresh(app, adapters, public_app)
    install(client, public_app)
    response = client.put(f"/api/v1/apps/{APP_ID}/source", json={"ref": ref, "ref_kind": "branch"}, headers=HEADERS)
    assert response.status_code == 200
    assert adapters.state.read_apps_state().apps[APP_ID].source.public_app_id == ""
    refresh(app, adapters)
    assert public_app.source.url is not None
    adapters.git.remote_commits[(public_app.source.url, ref, "branch")] = "c" * 40
    check = client.post(f"/api/v1/apps/{APP_ID}/update/check", headers=HEADERS)
    assert check.status_code == 200
    assert check.json()["remote_commit"] == "c" * 40
    assert adapters.git.fetch_commit_calls == [(public_app.source.url, ref, "branch")]
    response = client.post(f"/api/v1/apps/{APP_ID}/update", headers=HEADERS)
    assert response.status_code == 202
    assert adapters.git.clone_calls[-1] == (public_app.source.url, ref, "branch")
    assert adapters.state.read_apps_state().apps[APP_ID].source.public_app_id == ""


def test_periodic_sweep_skips_public_apps(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    install(client, public_app)
    state = adapters.state.read_apps_state()
    record = state.apps[APP_ID]
    adapters.state.write_apps_state(
        replace(state, apps={APP_ID: replace(record, source=replace(record.source, ref_kind="branch"))})
    )
    before = adapters.state.read_apps_state()
    run_git_check_sweep(app.state.apps_job_context, adapters.state)
    assert adapters.git.fetch_commit_calls == []
    assert adapters.state.read_apps_state() == before


@pytest.mark.parametrize("official, present", [(False, True), (True, True), (True, False)])
def test_public_official_badge_follows_cached_item(
    client: TestClient,
    app: FastAPI,
    adapters: FakeAdapterBundle,
    public_app: PublicCatalogApp,
    official: bool,
    present: bool,
) -> None:
    install(client, public_app)
    target = replace(public_app, public=replace(public_app.public, official=official))
    refresh(app, adapters, *((target,) if present else ()))
    assert client.get("/api/v1/apps").json()["apps"][0]["source"]["official"] is (official and present)
    assert client.get(f"/api/v1/apps/{APP_ID}").json()["source"]["official"] is (official and present)


def test_periodic_public_catalog_refresh_waits_for_ntp_and_one_hour(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle
) -> None:
    start = adapters.clock.monotonic_value
    scheduler = app.state.periodic_scheduler
    adapters.clock.monotonic_value = start + 3599
    scheduler.tick()
    assert adapters.public_catalog.fetch_calls == 0
    adapters.clock.synchronized = False
    adapters.clock.monotonic_value = start + 3600
    scheduler.tick()
    assert adapters.public_catalog.fetch_calls == 0
    adapters.clock.synchronized = True
    scheduler.tick()
    assert adapters.public_catalog.fetch_calls == 1


@pytest.mark.parametrize("failure", ["fetch", "manifest"])
def test_public_install_failure_leaves_no_app_or_staging(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp, failure: str
) -> None:
    if failure == "fetch":
        adapters.git.raise_on_clone = GitCommandError("unreachable commit")
    else:
        adapters.git.on_clone = lambda dest, *args: dest.mkdir(parents=True)
    response = client.post(
        "/api/v1/apps/install-public",
        json={"public_app_id": public_app.public.app_id, "commit": public_app.source.commit},
        headers=HEADERS,
    )
    assert response.status_code == 422
    assert adapters.state.read_apps_state().apps == {}
    assert list(app.state.apps_job_context.staging_dir.glob("*")) == []


@pytest.mark.parametrize("failure", ["fetch", "manifest", "sync"])
def test_public_update_failure_preserves_installed_source_and_files(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp, failure: str
) -> None:
    install(client, public_app)
    previous = adapters.state.read_apps_state().apps[APP_ID]
    root = app.state.apps_job_context.app_dir(APP_ID)
    (root / "keep.txt").write_text("previous")
    target = replace(
        public_app, source=replace(public_app.source, url="https://github.com/new-owner/new-repo", commit="d" * 40)
    )
    refresh(app, adapters, target)
    if failure == "fetch":
        adapters.git.raise_on_clone = GitCommandError("unreachable commit")
    elif failure == "manifest":
        adapters.git.on_clone = lambda dest, *args: dest.mkdir(parents=True)
    else:
        adapters.sync_unit.default_status = UnitStatus("failed", "failed", "exit-code", 1)
    response = client.post(f"/api/v1/apps/{APP_ID}/update", json={"commit": target.source.commit}, headers=HEADERS)
    assert response.status_code == (202 if failure == "sync" else 422)
    if failure == "sync":
        assert response.json()["job"]["state"] == "failed"
    after = adapters.state.read_apps_state().apps[APP_ID]
    assert after.source == previous.source
    assert after.params == previous.params
    assert (root / "keep.txt").read_text() == "previous"
    assert list(app.state.apps_job_context.staging_dir.glob("*")) == []


def test_install_public_uses_cached_success_during_catalog_outage(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    client.get("/api/v1/catalog/public")
    adapters.public_catalog.raise_on_fetch = PublicCatalogSourceError("offline")
    app.state.public_catalog_cache.refresh(ntp_synchronized=True)
    install(client, public_app)
    assert adapters.state.read_apps_state().apps[APP_ID].source.public_app_id == public_app.public.app_id


def test_public_install_chooses_unused_id_under_catalog_namespace(
    client: TestClient, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    install(client, public_app)
    previous = adapters.state.read_apps_state().apps[APP_ID]
    install(client, public_app)
    state = adapters.state.read_apps_state()
    assert state.apps[APP_ID] == previous
    assert state.apps[f"{APP_ID}-2"].source.public_app_id == public_app.public.app_id


@pytest.mark.parametrize("method, path", [("GET", "/api/v1/catalog/public"), ("POST", "/api/v1/apps/install-public")])
def test_public_routes_require_authentication(
    client: TestClient, adapters: FakeAdapterBundle, method: str, path: str
) -> None:
    client.cookies.clear()
    response = client.request(method, path, headers=HEADERS)
    assert response.status_code == 401
    assert adapters.public_catalog.fetch_calls == 0
    assert adapters.git.clone_commit_calls == []


def test_public_catalog_endpoint_refreshes_after_expiry(
    client: TestClient, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    before = client.get("/api/v1/catalog/public").json()
    target = replace(public_app, source=replace(public_app.source, commit="e" * 40))
    adapters.public_catalog.asset = PublicCatalogAsset("2026-11-09T02:00:00Z", (target,))
    assert client.get("/api/v1/catalog/public").json()["apps"] == before["apps"]
    adapters.clock.monotonic_value += 3600
    response = client.get("/api/v1/catalog/public").json()
    assert response["apps"][0]["source"]["commit"] == target.source.commit
    assert response["generated_at"] == "2026-11-09T02:00:00Z"
    assert adapters.public_catalog.fetch_calls == 2


def test_public_install_requires_csrf_header(
    client: TestClient, public_app: PublicCatalogApp, adapters: FakeAdapterBundle
) -> None:
    response = client.post(
        "/api/v1/apps/install-public",
        json={"public_app_id": public_app.public.app_id, "commit": public_app.source.commit},
    )
    assert response.status_code == 403
    assert adapters.git.clone_commit_calls == []
    assert adapters.state.read_apps_state().apps == {}


def test_public_update_does_not_clear_another_apps_rejected_credential(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    install(client, public_app)
    state = adapters.state.read_apps_state()
    private = replace(
        state.apps[APP_ID],
        id="alice.private",
        source=replace(public_app.source, public_app_id=None),
        credential_rejected=True,
    )
    adapters.state.write_apps_state(replace(state, apps={**state.apps, private.id: private}))
    refresh(app, adapters, replace(public_app, source=replace(public_app.source, commit="a" * 40)))
    response = client.post(f"/api/v1/apps/{APP_ID}/update", json={"commit": "a" * 40}, headers=HEADERS)
    assert response.status_code == 202
    assert response.json()["job"]["state"] == "done"
    assert adapters.state.read_apps_state().apps[private.id].credential_rejected


def test_periodic_credential_rejection_does_not_mark_public_apps(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    install(client, public_app)
    state = adapters.state.read_apps_state()
    private = replace(
        state.apps[APP_ID], id="alice.private", source=replace(public_app.source, public_app_id=None, ref_kind="branch")
    )
    adapters.state.write_apps_state(replace(state, apps={**state.apps, private.id: private}))
    adapters.secrets.set_git_credential("github.com/alice", "revoked")
    adapters.git.raise_on_fetch_commit = GitCommandError("rejected", status_code=401)
    run_git_check_sweep(app.state.apps_job_context, adapters.state)
    after = adapters.state.read_apps_state()
    assert after.apps[private.id].credential_rejected
    assert not after.apps[APP_ID].credential_rejected


@pytest.mark.parametrize(
    "cache_text", [None, "not json", '{"schema":1,"generated_at":"2026-11-09T00:00:00Z","apps":[{}]}']
)
def test_public_state_is_unknown_when_starting_without_valid_cache(
    client: TestClient,
    app: FastAPI,
    adapters: FakeAdapterBundle,
    public_app: PublicCatalogApp,
    tmp_path: Path,
    cache_text: str | None,
) -> None:
    from palmimo_portal.adapters.state import JsonFileStateStore
    from palmimo_portal.core.public_catalog import PublicCatalogCache

    install(client, public_app)
    if cache_text is not None:
        (tmp_path / "public_catalog_cache.json").write_text(cache_text)
    cache = PublicCatalogCache(adapters.public_catalog, JsonFileStateStore(tmp_path))
    app.state.public_catalog_cache = cache
    app.state.apps_job_context.public_catalog_cache = cache
    body = client.get(f"/api/v1/apps/{APP_ID}").json()
    assert body["public_app_available"] is None
    assert body["source"]["official"] is None
    assert body["update_available"] is None
    adapters.public_catalog.raise_on_fetch = PublicCatalogSourceError("offline")
    checked = client.post(f"/api/v1/apps/{APP_ID}/update/check", headers=HEADERS).json()
    assert checked["public_app_available"] is None
    assert checked["update_available"] is None
    response = client.post(f"/api/v1/apps/{APP_ID}/update", json={"commit": public_app.source.commit}, headers=HEADERS)
    assert response.status_code == 503


@pytest.mark.parametrize("commit", [None, "f" * 40])
def test_public_update_rejects_an_unconfirmed_commit(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp, commit: str | None
) -> None:
    install(client, public_app)
    refresh(app, adapters, replace(public_app, source=replace(public_app.source, commit="b" * 40)))
    before = adapters.state.read_apps_state()
    response = client.post(f"/api/v1/apps/{APP_ID}/update", json={"commit": commit} if commit else {}, headers=HEADERS)
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "public_app_changed"
    assert adapters.state.read_apps_state() == before


@pytest.mark.parametrize("operation", ["install", "update"])
@pytest.mark.parametrize("field", ["devices", "env"])
def test_public_manifest_mismatch_is_rejected_before_registration(
    client: TestClient,
    app: FastAPI,
    adapters: FakeAdapterBundle,
    public_app: PublicCatalogApp,
    operation: str,
    field: str,
) -> None:
    if operation == "update":
        install(client, public_app)
    before = adapters.state.read_apps_state()
    seed = adapters.git.on_clone
    assert seed is not None

    def mismatch(dest: Path, url: str, ref: str, kind: str) -> None:
        seed(dest, url, ref, kind)
        path = dest / (public_app.source.subdir or "") / "palmimo.toml"
        content = path.read_text()
        path.write_text(
            content.replace("motor_display", "camera")
            if field == "devices"
            else content.replace("API_KEY", "OTHER_KEY")
        )

    adapters.git.on_clone = mismatch
    endpoint = "/api/v1/apps/install-public" if operation == "install" else f"/api/v1/apps/{APP_ID}/update"
    response = client.post(
        endpoint, json={"public_app_id": public_app.public.app_id, "commit": public_app.source.commit}, headers=HEADERS
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "public_app_mismatch"
    assert adapters.state.read_apps_state() == before
    assert list(app.state.apps_job_context.staging_dir.glob("*")) == []


def test_public_update_check_refreshes_expired_catalog(
    client: TestClient, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    install(client, public_app)
    adapters.clock.monotonic_value += 3600
    adapters.public_catalog.asset = PublicCatalogAsset(
        "2026-11-09T01:00:00Z", (replace(public_app, source=replace(public_app.source, commit="b" * 40)),)
    )
    response = client.post(f"/api/v1/apps/{APP_ID}/update/check", headers=HEADERS)
    assert response.json()["remote_commit"] == "b" * 40
    assert adapters.public_catalog.fetch_calls == 2


def test_public_official_source_cannot_be_edited(
    client: TestClient, app: FastAPI, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    install(client, public_app)
    refresh(app, adapters, replace(public_app, public=replace(public_app.public, official=True)))
    before = adapters.state.read_apps_state()
    response = client.put(f"/api/v1/apps/{APP_ID}/source", json={"ref": "other", "ref_kind": "branch"}, headers=HEADERS)
    assert response.status_code == 409
    assert adapters.state.read_apps_state() == before


def test_public_install_reports_unsynchronized_clock(
    client: TestClient, adapters: FakeAdapterBundle, public_app: PublicCatalogApp
) -> None:
    adapters.clock.synchronized = False
    response = client.post(
        "/api/v1/apps/install-public",
        json={"public_app_id": public_app.public.app_id, "commit": public_app.source.commit},
        headers=HEADERS,
    )
    assert response.status_code == 503
    assert response.json()["error"]["params"]["reason"] == "clock_unsynced"


def test_public_page_links_use_the_configured_catalog_origin(
    client: TestClient, app: FastAPI, public_app: PublicCatalogApp
) -> None:
    app.state.apps_job_context.public_catalog_url = "https://catalog.example/custom/catalog.json"
    item = client.get("/api/v1/catalog/public").json()["apps"][0]
    assert item["public_page_url"] == f"https://catalog.example/apps/{public_app.public.app_id}"
    install(client, public_app)
    assert client.get(f"/api/v1/apps/{APP_ID}").json()["source"]["public_page_url"] == item["public_page_url"]
