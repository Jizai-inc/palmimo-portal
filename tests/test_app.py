"""Tests for :mod:`palmimo_portal.api.app`: bootstrap concerns beyond one router.

Covers the operational hardening that does not belong to any single
``api/`` module: hiding ``/docs`` by default, the startup banner, and
HostGuard's lazy re-resolution of this machine's hostnames/IPs.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.api.app import HostGuardMiddleware, create_app
from palmimo_portal.settings import Settings


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


def _built_static_dir(tmp_path: Path) -> Path:
    """A minimal fake `make build` output: enough for `_mount_frontend` to mount it."""
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html><title>Palmimo DevKit</title>", encoding="utf-8")
    (static_dir / "assets" / "index.js").write_text("// built asset\n", encoding="utf-8")
    return static_dir


def test_docs_are_hidden_by_default(client: TestClient) -> None:
    response = client.get("/docs")

    assert response.status_code == 404


def test_redoc_is_hidden_by_default(client: TestClient) -> None:
    response = client.get("/redoc")

    assert response.status_code == 404


def test_openapi_schema_is_hidden_by_default(client: TestClient) -> None:
    response = client.get("/openapi.json")

    assert response.status_code == 404


def test_docs_are_served_when_enabled() -> None:
    settings = Settings(allowed_hosts=frozenset({"testserver"}), enable_docs=True)
    app = create_app(settings)
    client = TestClient(app)

    response = client.get("/docs")

    assert response.status_code == 200


def test_frontend_is_not_mounted_when_static_dir_is_missing(client: TestClient) -> None:
    # The `client`/`settings` fixtures (conftest.py) point static_dir at a
    # directory that does not exist, so an unmatched path 404s the ordinary
    # way (no route ever matches it, so this never reaches _mount_frontend's
    # own catch-all route at all) rather than falling into the SPA fallback.
    response = client.get("/some-unmatched-path")

    assert response.status_code == 404


def test_missing_static_dir_logs_a_warning_naming_the_fix(caplog: pytest.LogCaptureFixture, tmp_path: Path) -> None:
    # The frontend build output is no longer committed (it ships as a
    # GitHub Release asset instead -- see doc/releasing.md), so a
    # source checkout with no `make build`/updater run yet must not fail
    # silently: this is the one message an operator sees in journalctl.
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=tmp_path / "does-not-exist")
    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        create_app(settings)

    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert any(
        "frontend build not found" in message
        and "make build" in message
        and "repository root" in message
        and "updater" in message
        for message in warnings
    )


def test_create_app_restores_static_from_static_prev_when_static_is_missing(tmp_path: Path) -> None:
    # A power loss between swap_into_place's two renames can leave static/
    # missing with the last-known-good build sitting in static.prev --
    # create_app (via wiring.build_adapters) must repair this before
    # _mount_frontend ever looks at static_dir, so the device does not 404
    # every page until an operator fixes it over SSH.
    static_dir = tmp_path / "static"
    prev_dir = tmp_path / "static.prev"
    (prev_dir / "assets").mkdir(parents=True)
    (prev_dir / "index.html").write_text("<!doctype html><title>Palmimo DevKit</title>", encoding="utf-8")
    (prev_dir / "assets" / "index.js").write_text("// built asset\n", encoding="utf-8")
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=static_dir)

    client = TestClient(create_app(settings))
    response = client.get("/")

    assert response.status_code == 200
    assert "Palmimo DevKit" in response.text
    assert not prev_dir.exists()


def test_frontend_root_serves_index_html(tmp_path: Path) -> None:
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=_built_static_dir(tmp_path))
    client = TestClient(create_app(settings))

    response = client.get("/")

    assert response.status_code == 200
    assert "Palmimo DevKit" in response.text


def test_frontend_serves_a_built_asset(tmp_path: Path) -> None:
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=_built_static_dir(tmp_path))
    client = TestClient(create_app(settings))

    response = client.get("/assets/index.js")

    assert response.status_code == 200
    assert "built asset" in response.text


def test_frontend_spa_fallback_serves_index_html_for_a_client_route(tmp_path: Path) -> None:
    # /login has no server-side route -- only a route the bundle registers
    # once index.html's script loads (see routes/login.tsx) -- so a direct
    # link or a hard refresh has to resolve to the same shell.
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=_built_static_dir(tmp_path))
    client = TestClient(create_app(settings))

    response = client.get("/login")

    assert response.status_code == 200
    assert "Palmimo DevKit" in response.text


def test_frontend_spa_fallback_still_404s_an_unmatched_api_path(tmp_path: Path) -> None:
    # A typo'd or removed API path must keep answering as the usual JSON
    # error envelope, never index.html -- the frontend router has no way to
    # make sense of it either.
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=_built_static_dir(tmp_path))
    client = TestClient(create_app(settings))

    response = client.get("/api/v1/does-not-exist")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


def test_frontend_spa_fallback_404s_the_bare_api_path(tmp_path: Path) -> None:
    # `/api` (no trailing slash, nothing after it) does not start with the
    # `"api/"` prefix the fallback checks for, so it used to fall through to
    # the static-file lookup, miss, and get served index.html with a 200 --
    # this is exactly as much an API path as `/api/v1/...` and must 404 as
    # the JSON error envelope too.
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=_built_static_dir(tmp_path))
    client = TestClient(create_app(settings))

    response = client.get("/api")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "not_found"


@pytest.mark.parametrize(
    "traversal_path",
    [
        "/..%2fsecret.txt",
        "/%2e%2e/secret.txt",
        "/assets/..%2fsecret.txt",
        "/%2e%2e%2fsecret.txt",
    ],
)
def test_spa_fallback_rejects_percent_encoded_path_traversal(tmp_path: Path, traversal_path: str) -> None:
    # A file that exists just outside static_dir -- e.g. a sibling
    # static.prev/ or a state file the swap-into-place machinery leaves
    # nearby -- must never be reachable through the SPA fallback's
    # candidate = static_dir / full_path lookup, encoded or not. The
    # unencoded ".." case is already covered indirectly by the
    # resolve().relative_to() guard; these percent-encoded variants prove
    # Starlette's own path-segment decoding does not let one slip past it
    # before that guard ever runs.
    outside_file = tmp_path / "secret.txt"
    outside_file.write_text("do-not-serve-me", encoding="utf-8")
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=_built_static_dir(tmp_path))
    client = TestClient(create_app(settings))

    response = client.get(traversal_path)

    assert response.status_code == 404
    assert "do-not-serve-me" not in response.text


def test_mount_frontend_degrades_to_api_only_when_assets_dir_is_missing(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    # An interrupted `make build` or a half-extracted release asset can
    # leave index.html present with assets/ missing -- create_app() must
    # not crash (StaticFiles(directory=...) raises at mount time against a
    # missing directory); it must degrade to "API only", the same as a
    # wholly missing static_dir.
    static_dir = tmp_path / "static"
    static_dir.mkdir()
    (static_dir / "index.html").write_text("<!doctype html><title>Palmimo DevKit</title>", encoding="utf-8")
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=static_dir)

    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        app = create_app(settings)  # must not raise
    client = TestClient(app)

    response = client.get("/api/v1/system/status")
    assert response.status_code == 200  # the API still works

    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert any("frontend build not found" in message for message in warnings)


def test_mount_frontend_degrades_to_api_only_when_assets_dir_is_empty(
    caplog: pytest.LogCaptureFixture, tmp_path: Path
) -> None:
    # An empty assets/ directory (present, but with nothing extracted into
    # it yet) is just as much a half-extracted build as assets/ being
    # missing outright -- StaticFiles would happily mount an empty
    # directory without raising, so without this check create_app() would
    # not crash, but the UI would still be broken (no JS to serve) with no
    # warning in journalctl to explain why.
    static_dir = tmp_path / "static"
    (static_dir / "assets").mkdir(parents=True)
    (static_dir / "index.html").write_text("<!doctype html><title>Palmimo DevKit</title>", encoding="utf-8")
    settings = Settings(allowed_hosts=frozenset({"testserver"}), static_dir=static_dir)

    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        app = create_app(settings)  # must not raise
    client = TestClient(app)

    response = client.get("/api/v1/system/status")
    assert response.status_code == 200  # the API still works

    warnings = [record.message for record in caplog.records if record.levelno == logging.WARNING]
    assert any("frontend build not found" in message for message in warnings)


def test_create_app_logs_a_startup_banner(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(allowed_hosts=frozenset({"testserver"}), port=9999)

    with caplog.at_level(logging.INFO, logger="palmimo_portal"):
        create_app(settings)

    assert "port=9999" in caplog.text
    assert "adapters=fake" in caplog.text


def test_create_app_warns_when_using_fake_adapters(caplog: pytest.LogCaptureFixture) -> None:
    settings = Settings(allowed_hosts=frozenset({"testserver"}))

    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        create_app(settings)

    fake_warnings = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert any("fake" in record.message for record in fake_warnings)


def test_hostguard_rejection_is_logged_at_warning(client: TestClient, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
        response = client.get("/api/v1/system/status", headers={"Host": "evil.example.com"})

    assert response.status_code == 421
    assert any("evil.example.com" in record.message for record in caplog.records)


def test_hostguard_reresolves_machine_hosts_after_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    import palmimo_portal.api.app as app_module

    calls = {"n": 0}

    def fake_machine_hosts() -> frozenset[str]:
        calls["n"] += 1
        return frozenset({"new-host"}) if calls["n"] > 1 else frozenset()

    monkeypatch.setattr(app_module, "_machine_hosts", fake_machine_hosts)

    clock = {"t": 0.0}
    middleware = HostGuardMiddleware(app=FastAPI(), always_allowed_hosts=frozenset(), clock=lambda: clock["t"])

    first = middleware._allowed_hosts()
    assert "new-host" not in first
    assert calls["n"] == 1

    # Within the TTL window, the cached (stale) result is reused.
    still_cached = middleware._allowed_hosts()
    assert "new-host" not in still_cached
    assert calls["n"] == 1

    clock["t"] += app_module._HOST_CACHE_TTL_SECONDS + 1.0
    refreshed = middleware._allowed_hosts()

    assert "new-host" in refreshed
    assert calls["n"] == 2


def test_hostguard_allows_a_request_whose_host_is_an_enumerated_interface_ip(
    monkeypatch: pytest.MonkeyPatch, settings: Settings
) -> None:
    # gethostbyname_ex-based resolution alone would miss this: on a Pi it
    # typically resolves only the /etc/hosts loopback alias, not a real
    # bound interface address like the AP-mode gateway IP a setup client
    # would use as its numeric-IP Host header. _interface_ipv4_addresses is
    # the SIOCGIFADDR-based enumerator that closes that gap; this proves
    # its result actually reaches HostGuard's allow-list end to end.
    import palmimo_portal.api.app as app_module

    monkeypatch.setattr(app_module, "_interface_ipv4_addresses", lambda: frozenset({"203.0.113.5"}))
    app = create_app(settings)
    client = TestClient(app, headers={"Host": "203.0.113.5"})

    response = client.get("/api/v1/system/status")

    assert response.status_code == 200


def test_interface_ipv4_addresses_does_not_raise_when_ioctl_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    import palmimo_portal.api.app as app_module

    def broken_if_nameindex() -> list[tuple[int, str]]:
        raise OSError("no network stack available")

    monkeypatch.setattr(app_module.socket, "if_nameindex", broken_if_nameindex)

    assert app_module._interface_ipv4_addresses() == frozenset()
