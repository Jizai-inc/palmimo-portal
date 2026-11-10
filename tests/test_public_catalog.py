from __future__ import annotations

import io
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from palmimo_portal.adapters.git_port import SubprocessGitPort
from palmimo_portal.adapters.public_catalog import MAX_CATALOG_BYTES, HttpPublicCatalogSource, parse_public_catalog
from palmimo_portal.adapters.state import JsonFileStateStore
from palmimo_portal.core.apps import app_id_from_public_catalog, app_namespace
from palmimo_portal.core.manifest import manifest_snapshot, parse_manifest
from palmimo_portal.core.public_catalog import PublicCatalogCache
from palmimo_portal.ports import AppRecord, AppSource, AppsState
from palmimo_portal.settings import get_settings


FIXTURES = Path(__file__).parent / "fixtures/public_catalog"


def catalog_bytes() -> bytes:
    return (FIXTURES / "catalog.example.json").read_bytes()


@pytest.mark.parametrize(
    "behavior", ["kept", "discarded", "downgraded_official", "dropped_fields", "normalized_translations"]
)
def test_public_catalog_matches_shared_expectations(behavior: str) -> None:
    raw = json.loads(catalog_bytes())["apps"]
    expected = json.loads((FIXTURES / "catalog.example.expected.json").read_text())
    apps = parse_public_catalog(catalog_bytes()).apps
    by_id = {app.public.app_id: app for app in apps}
    if behavior == "kept":
        assert [app.public.app_id for app in apps] == [item["app_id"] for item in expected[behavior]]
    elif behavior == "discarded":
        kept_indices = {item["index"] for item in expected["kept"]}
        assert set(range(len(raw))) - kept_indices == {item["index"] for item in expected[behavior]}
        for item in expected[behavior]:
            assert item["app_id"] not in by_id
    elif behavior == "downgraded_official":
        assert {
            app.public.app_id
            for item in expected["kept"]
            if raw[item["index"]]["public"]["official"] and not (app := by_id[item["app_id"]]).public.official
        } == {item["app_id"] for item in expected[behavior]}
    elif behavior == "dropped_fields":
        for item in expected[behavior]:
            app = by_id[item["app_id"]]
            path = item["path"]
            if path.startswith("env."):
                assert app.env[path.split(".")[1]].help_url is None
            elif path == "translations":
                assert app.translations == {}
            else:
                locale, *key = path.removeprefix("translations.").split(".")
                assert locale not in app.translations or ".".join(key) not in app.translations[locale]
    else:
        for item in expected[behavior]:
            assert by_id[item["app_id"]].translations == item["value"]


@pytest.mark.parametrize("failure", ["network", "bytes", "count", "envelope"])
def test_public_catalog_failure_preserves_persisted_success(tmp_path: Path, failure: str) -> None:
    body = [catalog_bytes()]

    def opener(*args: Any) -> io.BytesIO:
        if failure == "network" and not body:
            raise OSError("offline")
        return io.BytesIO(body[0])

    store = JsonFileStateStore(tmp_path)
    cache = PublicCatalogCache(HttpPublicCatalogSource(opener=opener), store, wall_clock=lambda: 123.0)
    cache.refresh(ntp_synchronized=True)
    before = cache.peek()
    if failure == "network":
        body.clear()
    elif failure == "bytes":
        body[0] = b" " * (MAX_CATALOG_BYTES + 1)
    elif failure == "count":
        data = json.loads(catalog_bytes())
        data["apps"] = [data["apps"][0]] * 2001
        body[0] = json.dumps(data).encode()
    else:
        body[0] = b"{}"
    cache.refresh(ntp_synchronized=True)
    after = cache.peek()
    assert after.apps == before.apps
    assert after.fetched_at == 123.0
    assert after.reason is not None
    restored = PublicCatalogCache(HttpPublicCatalogSource(opener=opener), store).peek()
    assert restored.apps == before.apps
    assert restored.generated_at == before.generated_at
    assert restored.fetched_at == 123.0


def test_public_catalog_request_refreshes_on_expiry_and_backs_off(tmp_path: Path) -> None:
    now = [0.0]
    calls: list[int] = []

    def opener(*args: Any) -> io.BytesIO:
        calls.append(1)
        if len(calls) > 1:
            raise OSError("offline")
        return io.BytesIO(catalog_bytes())

    cache = PublicCatalogCache(
        HttpPublicCatalogSource(opener=opener), JsonFileStateStore(tmp_path), monotonic=lambda: now[0]
    )
    assert cache.get(ntp_synchronized=False).generated_at is None
    assert calls == []
    assert cache.get(ntp_synchronized=True).generated_at is not None
    cache.get(ntp_synchronized=True)
    assert len(calls) == 1
    now[0] = 3600
    cache.get(ntp_synchronized=True)
    cache.get(ntp_synchronized=True)
    assert len(calls) == 2
    now[0] += 300
    cache.get(ntp_synchronized=True)
    assert len(calls) == 3


@pytest.mark.parametrize("value", [None, "", "019a3f2c-0000-7000-8000-000000000001"])
def test_public_app_source_round_trips_in_unchanged_ledger_schema(tmp_path: Path, value: str | None) -> None:
    store = JsonFileStateStore(tmp_path)
    record = AppRecord(
        "dance",
        AppSource(type="git", public_app_id=value),
        None,
        {},
        False,
        None,
        id="alice.dance",
        manifest=manifest_snapshot(
            parse_manifest('schema = 1\nname = "dance"\ndescription = "dance"\ncommand = ["run"]')
        ),
    )
    store.write_apps_state(AppsState(apps={record.id: record}))
    payload = json.loads((tmp_path / "apps.json").read_text())
    assert payload["schema"] == 1
    if value is None:
        assert "public_app_id" not in payload["apps"][record.id]["source"]
    assert store.read_apps_state().apps[record.id].source.public_app_id == value


def test_clone_commit_shallow_fetches_commit_without_named_ref(tmp_path: Path) -> None:
    remote = tmp_path / "remote.git"

    def git(*args: str) -> str:
        return subprocess.run(["git", *args], check=True, capture_output=True, text=True).stdout.strip()

    git("init", "--bare", str(remote))
    tree = git("--git-dir", str(remote), "mktree")
    sha = git(
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "--git-dir",
        str(remote),
        "commit-tree",
        tree,
        "-m",
        "Unreferenced",
    )
    dest = tmp_path / "checkout"
    assert SubprocessGitPort().clone_commit_shallow(remote.as_uri(), sha, dest) == sha
    assert git("-C", str(dest), "rev-parse", "HEAD") == sha
    assert (dest / ".git/shallow").is_file()


@pytest.mark.parametrize("occupied, expected", [(None, "palmimo.teleop"), ("palmimo.teleop", "palmimo.teleop-2")])
def test_catalog_app_id_uses_public_namespace_and_existing_collision_rule(occupied: str | None, expected: str) -> None:
    app = parse_public_catalog(catalog_bytes()).apps[1]
    state = AppsState(current_job_app=occupied)
    assert app_id_from_public_catalog(app, state) == expected


def test_cloud_namespace_is_reserved_for_github_sources() -> None:
    assert app_namespace("git", "https://github.com/cloud/dance") == "cloud-gh"


def test_public_catalog_url_is_configurable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALMIMO_PUBLIC_CATALOG_URL", "https://example.com/catalog.json")
    assert get_settings().public_catalog_url == "https://example.com/catalog.json"


def test_public_catalog_empty_success_replaces_old_cache(tmp_path: Path) -> None:
    body = [catalog_bytes()]
    cache = PublicCatalogCache(
        HttpPublicCatalogSource(opener=lambda *args: io.BytesIO(body[0])), JsonFileStateStore(tmp_path)
    )
    cache.refresh(ntp_synchronized=True)
    data = json.loads(body[0])
    data["apps"] = []
    body[0] = json.dumps(data).encode()
    cache.refresh(ntp_synchronized=True)
    snapshot = cache.get(ntp_synchronized=True)
    assert snapshot.generated_at == data["generated_at"]
    assert snapshot.apps == ()
    assert not snapshot.stale
    assert snapshot.reason is None
    restored = PublicCatalogCache(HttpPublicCatalogSource(), JsonFileStateStore(tmp_path)).peek()
    assert restored.generated_at == snapshot.generated_at
    assert restored.apps == ()


def test_public_catalog_unicode_survives_persistent_encoding_growth(tmp_path: Path) -> None:
    data = json.loads(catalog_bytes())
    data["apps"] = [data["apps"][0]]
    data["apps"][0]["description"] = "あ" * 900_000
    body = json.dumps(data, ensure_ascii=False).encode()
    assert len(body) < MAX_CATALOG_BYTES
    store = JsonFileStateStore(tmp_path)
    cache = PublicCatalogCache(HttpPublicCatalogSource(opener=lambda *args: io.BytesIO(body)), store)
    cache.refresh(ntp_synchronized=True)
    restored = PublicCatalogCache(HttpPublicCatalogSource(), store).peek()
    assert restored.apps == cache.peek().apps
    assert restored.generated_at == cache.peek().generated_at


def test_public_catalog_recovers_after_failed_first_fetch(tmp_path: Path) -> None:
    body = [b"{}"]
    cache = PublicCatalogCache(
        HttpPublicCatalogSource(opener=lambda *args: io.BytesIO(body[0])), JsonFileStateStore(tmp_path)
    )
    cache.refresh(ntp_synchronized=True)
    assert cache.peek().generated_at is None
    assert cache.peek().stale
    body[0] = catalog_bytes()
    cache.refresh(ntp_synchronized=True)
    assert cache.peek().generated_at is not None
    assert not cache.peek().stale
    assert cache.peek().reason is None


def test_public_catalog_persistence_failure_keeps_fetched_data_in_memory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = JsonFileStateStore(tmp_path)

    def fail_write(state: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(store, "write_public_catalog_cache", fail_write)
    cache = PublicCatalogCache(HttpPublicCatalogSource(opener=lambda *args: io.BytesIO(catalog_bytes())), store)
    cache.refresh(ntp_synchronized=True)
    snapshot = cache.peek()
    assert snapshot.apps == parse_public_catalog(catalog_bytes()).apps
    assert snapshot.generated_at is not None
    assert snapshot.fetched_at is not None
    assert snapshot.reason == "persistence_failed"
