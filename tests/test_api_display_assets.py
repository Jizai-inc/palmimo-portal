from __future__ import annotations

from collections.abc import Iterator
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.api.app import create_app
from palmimo_portal.ports import (
    AppAssetsStatus,
    DisplayAssetConversion,
    DisplayAssetMessage,
    DisplayAssetsStatus,
    DisplaySyncPending,
    DisplaySyncResult,
    DisplayToolError,
    PlatformCommandError,
    StandardAssetStatus,
)
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle, FakeDisplaySyncService, FakePlatformPort


HEADERS = {"X-Requested-With": "PalmimoPortal"}
BASE = "/api/v1/display-assets"
STANDARD = StandardAssetStatus("happy", 4096, "vector", ("color",), False, 4096, "vector", ("color",))


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        allowed_hosts=frozenset({"testserver"}),
        state_dir=tmp_path / "state",
        static_dir=tmp_path / "static",
        periodic_enabled=False,
        start_run_in_thread=False,
    )


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def client(app: FastAPI, adapters: FakeAdapterBundle) -> Iterator[TestClient]:
    with TestClient(app) as client:
        client.post("/api/v1/auth/setup", json={"password": "hunter2"}, headers=HEADERS)
        adapters.saved_networks.save("home", "", first=False)
        client.post("/api/v1/auth/login", json={"password": "hunter2"}, headers=HEADERS)
        adapters.standard_assets.names = ("happy", "system.idle")
        adapters.platform.display_assets = DisplayAssetsStatus(
            32768,
            (STANDARD, replace(STANDARD, name="system.idle")),
            {"zip.small": AppAssetsStatus(4096, 4096, ("wave",))},
            ("retired",),
        )
        adapters.asset_converter.result = DisplayAssetConversion(True, "gif", 8192, "12345678", {}, (), ())
        adapters.asset_converter.content = b"converted"
        yield client


def _preview(client: TestClient, name: str = "happy") -> dict:
    response = client.post(f"{BASE}/standard/{name}/preview", files={"file": ("face.gif", b"source")}, headers=HEADERS)
    assert response.status_code == 200, response.text
    return response.json()


@pytest.mark.parametrize("operation", ["put", "delete"])
@pytest.mark.parametrize(
    "failure", [PlatformCommandError("display-assets", "timeout"), DisplayToolError("invalid output")]
)
def test_saved_override_reports_sync_exception_and_leaves_pending_retry(
    client: TestClient, adapters: FakeAdapterBundle, monkeypatch: pytest.MonkeyPatch, operation: str, failure: Exception
) -> None:
    token = _preview(client)["token"] if operation == "put" else None

    def fail_sync(app_id: str | None) -> DisplaySyncResult:
        raise failure

    monkeypatch.setattr(adapters.platform, "sync_display_assets", fail_sync)
    if operation == "put":
        response = client.put(f"{BASE}/standard/happy", json={"token": token}, headers=HEADERS)
    else:
        response = client.delete(f"{BASE}/standard/happy", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["sync"]["result"] == "failed"
    assert len(adapters.platform.override_display_asset_calls) == 1
    status = client.get("/api/v1/system/status").json()["display_sync"]
    assert status["pending"] is True
    assert status["reason"] == response.json()["sync"]["reason"]
    assert status["running"] is False
    if operation == "put":
        repeated = client.put(f"{BASE}/standard/happy", json={"token": token}, headers=HEADERS)
        assert repeated.status_code == 409
        assert repeated.json()["error"]["code"] == "display_asset_preview_expired"
        assert len(adapters.platform.override_display_asset_calls) == 1


def test_standard_list_preserves_catalog_translations_and_includes_uncatalogued_assets(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    response = client.get(f"{BASE}/standard")

    assert response.status_code == 200
    rows = {row["name"]: row for row in response.json()["standard"]}
    entry = next(entry for entry in adapters.standard_assets.catalog["assets"] if entry["id"] == "palmimo/happy")
    assert rows["happy"]["description"] == entry["description"]
    assert rows["happy"]["translations"] == entry["translations"]
    assert "display_name" not in rows["happy"]
    assert rows["system.idle"]["description"] is None
    assert rows["system.idle"]["translations"] == {}
    assert rows["happy"]["default_params"] == ["color"]
    assert response.json()["orphan_overrides"] == ["retired"]


def test_preview_put_roundtrip_uses_converted_asset_and_consumes_token(
    client: TestClient, adapters: FakeAdapterBundle, monkeypatch: pytest.MonkeyPatch
) -> None:
    preview = _preview(client)
    assert preview["params_lost"] == ["color"]
    assert preview["kind"] == "gif"
    assert preview["bytes"] == 8192
    assert preview["crc32"] == "12345678"
    assert preview["capacity"]["ok"] is True
    captured = []

    def save(name: str, path: Path | None) -> None:
        assert name == "happy" and path is not None
        captured.append(path.read_bytes())

    monkeypatch.setattr(adapters.platform, "override_display_asset", save)
    response = client.put(f"{BASE}/standard/happy", json={"token": preview["token"]}, headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["sync"]["result"] == "synced"
    assert captured == [b"converted"]
    assert adapters.platform.sync_display_assets_calls == [None]
    reused = client.put(f"{BASE}/standard/happy", json={"token": preview["token"]}, headers=HEADERS)
    assert reused.status_code == 409
    assert reused.json()["error"]["code"] == "display_asset_preview_expired"


@pytest.mark.parametrize("token_state", ["missing", "expired", "other-name", "traversal"])
def test_put_refuses_unusable_preview_without_override(
    client: TestClient, adapters: FakeAdapterBundle, token_state: str
) -> None:
    preview = _preview(client)
    token = preview["token"]
    name = "happy"
    if token_state == "expired":
        adapters.clock.monotonic_value += 600
    elif token_state == "missing":
        token = "0" * 32
    elif token_state == "other-name":
        name = "system.idle"
    else:
        token = "../../outside"
    response = client.put(f"{BASE}/standard/{name}", json={"token": token}, headers=HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "display_asset_preview_expired"
    assert adapters.platform.override_display_asset_calls == []


def test_put_rechecks_capacity_against_apps_added_after_preview(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    token = _preview(client)["token"]
    status = adapters.platform.display_assets
    adapters.platform.display_assets = replace(status, apps={"zip.large": AppAssetsStatus(24576, 8192, ("large",))})

    response = client.put(f"{BASE}/standard/happy", json={"token": token}, headers=HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "display_capacity_exceeded"
    assert response.json()["error"]["params"]["short_by"] == 12288
    assert adapters.platform.override_display_asset_calls == []


def test_preview_reports_apps_that_would_no_longer_fit(client: TestClient, adapters: FakeAdapterBundle) -> None:
    status = adapters.platform.display_assets
    adapters.platform.display_assets = replace(
        status, apps={"zip.large": AppAssetsStatus(24576, 8192, ("large",)), **status.apps}
    )

    preview = _preview(client)

    assert preview["capacity"] == {"ok": False, "short_by": 12288, "apps_no_longer_fit": ["zip.large"]}


@pytest.mark.parametrize("name", ["happy", "retired"])
def test_delete_resets_standard_or_orphan_override(client: TestClient, adapters: FakeAdapterBundle, name: str) -> None:
    response = client.delete(f"{BASE}/standard/{name}", headers=HEADERS)

    assert response.status_code == 200
    assert adapters.platform.override_display_asset_calls == [(name, None)]
    assert adapters.platform.sync_display_assets_calls == [None]


@pytest.mark.parametrize("method", ["preview", "put"])
def test_unknown_standard_name_never_calls_privileged_tool(
    client: TestClient, adapters: FakeAdapterBundle, method: str
) -> None:
    path = f"{BASE}/standard/unknown"
    if method == "preview":
        response = client.post(f"{path}/preview", files={"file": ("face.gif", b"source")}, headers=HEADERS)
    else:
        response = client.put(path, json={"token": "unused"}, headers=HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "display_asset_unknown"
    assert adapters.platform.display_assets_status_calls == 0
    assert adapters.platform.override_display_asset_calls == []
    assert adapters.asset_converter.convert_calls == []


def test_delete_rejects_name_absent_from_standards_and_orphans(client: TestClient, adapters: FakeAdapterBundle) -> None:
    response = client.delete(f"{BASE}/standard/unknown", headers=HEADERS)

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "display_asset_unknown"
    assert adapters.platform.override_display_asset_calls == []


@pytest.mark.parametrize("lock", ["apps", "run"])
def test_put_refuses_lock_contention_without_override(
    client: TestClient, adapters: FakeAdapterBundle, lock: str
) -> None:
    token = _preview(client)["token"]
    guard = adapters.state.lock_apps if lock == "apps" else adapters.state.lock_run
    with guard():
        response = client.put(f"{BASE}/standard/happy", json={"token": token}, headers=HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "app_busy"
    assert adapters.platform.override_display_asset_calls == []


def test_preview_conversion_errors_are_all_returned(client: TestClient, adapters: FakeAdapterBundle) -> None:
    errors = (
        DisplayAssetMessage("unsupported", "unsupported", "$.a"),
        DisplayAssetMessage("invalid", "invalid", "$.b"),
    )
    adapters.asset_converter.result = DisplayAssetConversion(False, None, None, None, {}, (), errors)

    response = client.post(f"{BASE}/standard/happy/preview", files={"file": ("bad.json", b"bad")}, headers=HEADERS)

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "display_asset_invalid"
    assert [error["code"] for error in response.json()["error"]["params"]["errors"]] == ["unsupported", "invalid"]
    assert adapters.platform.display_assets_status_calls == 0


def test_oversized_upload_is_rejected_before_conversion(client: TestClient, adapters: FakeAdapterBundle) -> None:
    response = client.post(
        f"{BASE}/standard/happy/preview", files={"file": ("face.gif", b"x" * (16 * 1024 * 1024 + 1))}, headers=HEADERS
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "display_asset_invalid"
    assert adapters.asset_converter.convert_calls == []


@pytest.mark.parametrize("stage", ["status", "override"])
def test_tool_failure_after_name_validation_returns_500(
    client: TestClient, adapters: FakeAdapterBundle, stage: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _preview(client)["token"]
    if stage == "status":
        adapters.platform.display_error = DisplayToolError("private reason")
    else:

        def fail(name: str, path: Path | None) -> None:
            raise DisplayToolError("private reason")

        monkeypatch.setattr(adapters.platform, "override_display_asset", fail)

    response = client.put(f"{BASE}/standard/happy", json={"token": token}, headers=HEADERS)

    assert response.status_code == 500
    assert "private reason" not in response.text


@pytest.mark.parametrize("result", ["synced", "busy", "skipped", "failed"])
def test_manual_sync_returns_result_and_exposes_pending_status(
    client: TestClient, adapters: FakeAdapterBundle, result: str
) -> None:
    adapters.state.write_display_sync(DisplaySyncPending(True, "previous", "boot"))
    adapters.platform.display_sync_result = DisplaySyncResult.from_payload({"result": result, "reason": result})

    response = client.post(f"{BASE}/sync", headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["result"] == result
    assert adapters.platform.sync_display_assets_calls == [None]
    status = client.get("/api/v1/system/status").json()["display_sync"]
    assert status["running"] is False
    assert status["pending"] is (result != "synced")


@pytest.mark.parametrize(("size", "ok", "short_by"), [(8192, True, 0), (8193, False, 8192)])
def test_preview_capacity_rounds_assets_and_reserves_largest(
    client: TestClient, adapters: FakeAdapterBundle, size: int, ok: bool, short_by: int
) -> None:
    adapters.asset_converter.result = replace(adapters.asset_converter.result, bytes=size)
    adapters.platform.display_assets = replace(adapters.platform.display_assets, capacity=24576)

    result = _preview(client)["capacity"]

    assert result["ok"] is ok
    assert result["short_by"] == short_by


def test_preview_preserves_warning_codes_and_retained_slots(client: TestClient, adapters: FakeAdapterBundle) -> None:
    adapters.asset_converter.result = replace(
        adapters.asset_converter.result,
        kind="vector",
        params={"color": 3},
        warnings=(DisplayAssetMessage("approximation", "approximation"),),
    )

    preview = _preview(client)

    assert preview["params_lost"] == []
    assert [warning["code"] for warning in preview["warnings"]] == ["approximation"]


def test_saved_override_with_busy_sync_sets_pending_and_returns_success(
    client: TestClient, adapters: FakeAdapterBundle
) -> None:
    token = _preview(client)["token"]
    adapters.platform.display_sync_result = DisplaySyncResult("busy", (), (), "occupied")

    response = client.put(f"{BASE}/standard/happy", json={"token": token}, headers=HEADERS)

    assert response.status_code == 200
    assert response.json()["sync"]["result"] == "busy"
    assert len(adapters.platform.override_display_asset_calls) == 1
    status = client.get("/api/v1/system/status").json()["display_sync"]
    assert status == {"pending": True, "running": False, "reason": "occupied"}


@pytest.mark.parametrize("operation", ["list", "preview", "delete", "sync"])
def test_asset_operations_refuse_run_lock_contention(
    client: TestClient, adapters: FakeAdapterBundle, operation: str
) -> None:
    with adapters.state.lock_run():
        if operation == "list":
            response = client.get(f"{BASE}/standard")
        elif operation == "preview":
            response = client.post(f"{BASE}/standard/happy/preview", files={"file": ("a.gif", b"a")}, headers=HEADERS)
        elif operation == "delete":
            response = client.delete(f"{BASE}/standard/happy", headers=HEADERS)
        else:
            response = client.post(f"{BASE}/sync", headers=HEADERS)

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "app_busy"
    assert adapters.platform.display_assets_status_calls == 0
    assert adapters.platform.override_display_asset_calls == []
    assert adapters.platform.sync_display_assets_calls == []


def test_status_reports_running_only_while_sync_is_active(
    client: TestClient, adapters: FakeAdapterBundle, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    import threading
    from concurrent.futures import ThreadPoolExecutor

    entered, release = threading.Event(), threading.Event()
    platform = FakePlatformPort()
    app.state.display_sync = FakeDisplaySyncService(platform, adapters.state)

    def sync(app_id: str | None) -> DisplaySyncResult:
        entered.set()
        assert release.wait(5)
        return DisplaySyncResult("synced", (), (), None)

    monkeypatch.setattr(platform, "sync_display_assets", sync)
    with ThreadPoolExecutor() as pool:
        response = pool.submit(client.post, f"{BASE}/sync", headers=HEADERS)
        try:
            assert entered.wait(5)
            assert client.get("/api/v1/system/status").json()["display_sync"]["running"] is True
        finally:
            release.set()
        assert response.result(timeout=5).status_code == 200
    assert client.get("/api/v1/system/status").json()["display_sync"]["running"] is False


@pytest.mark.parametrize("operation", ["put", "delete", "sync"])
def test_asset_mutations_use_shared_sync_without_reacquiring_locks(
    client: TestClient, adapters: FakeAdapterBundle, app: FastAPI, operation: str
) -> None:
    class LockingSyncService(FakeDisplaySyncService):
        def sync(self) -> DisplaySyncResult:
            with self.state.lock_apps(), self.state.lock_run():
                return self.sync_locked()

    platform = FakePlatformPort(display_sync_result=DisplaySyncResult("busy", (), (), "occupied"))
    app.state.display_sync = LockingSyncService(platform, adapters.state)
    if operation == "put":
        token = _preview(client)["token"]
        response = client.put(f"{BASE}/standard/happy", json={"token": token}, headers=HEADERS)
    elif operation == "delete":
        response = client.delete(f"{BASE}/standard/happy", headers=HEADERS)
    else:
        response = client.post(f"{BASE}/sync", headers=HEADERS)

    assert response.status_code == 200
    payload = response.json() if operation == "sync" else response.json()["sync"]
    assert payload["result"] == "busy"
    assert platform.sync_display_assets_calls == [None]
    assert adapters.platform.sync_display_assets_calls == []


def test_missing_sync_service_refuses_save_before_mutation(
    client: TestClient, adapters: FakeAdapterBundle, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    token = _preview(client)["token"]
    monkeypatch.setattr(app.state, "display_sync", None, raising=False)
    app.state.adapters = replace(adapters, display_sync=None)

    response = client.put(f"{BASE}/standard/happy", json={"token": token}, headers=HEADERS)

    assert response.status_code == 503
    assert adapters.platform.override_display_asset_calls == []


def test_preview_rejects_multiple_files_before_conversion(client: TestClient, adapters: FakeAdapterBundle) -> None:
    response = client.post(
        f"{BASE}/standard/happy/preview",
        files=[("file", ("one.gif", b"one")), ("file", ("two.gif", b"two"))],
        headers=HEADERS,
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "display_asset_invalid"
    assert adapters.asset_converter.convert_calls == []
