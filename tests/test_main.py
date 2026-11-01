"""Tests for :mod:`palmimo_portal.__main__`'s logging setup."""

from __future__ import annotations

import logging
import socket
import threading
import time
from collections.abc import Iterator
from typing import cast

import httpx
import pytest
import uvicorn
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.__main__ import _configure_logging, server_config
from palmimo_portal.core.auth import setup_password
from palmimo_portal.ports import Identity
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


@pytest.fixture(autouse=True)
def _reset_root_logger() -> Iterator[None]:
    root = logging.getLogger()
    original_level = root.level
    original_handlers = list(root.handlers)
    yield
    root.setLevel(original_level)
    root.handlers = original_handlers


def test_configure_logging_defaults_to_info(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALMIMO_LOG_LEVEL", raising=False)

    _configure_logging()

    assert logging.getLogger().getEffectiveLevel() == logging.INFO


def test_configure_logging_honors_palmimo_log_level(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALMIMO_LOG_LEVEL", "DEBUG")

    _configure_logging()

    assert logging.getLogger().getEffectiveLevel() == logging.DEBUG


def test_server_preserves_peer_address_and_reset_budget_with_forwarded_headers(
    settings: Settings, caplog: pytest.LogCaptureFixture
) -> None:
    config = server_config(settings)
    app = cast(FastAPI, config.app)
    adapters = cast(FakeAdapterBundle, app.state.adapters)
    adapters.identity.identity = Identity(device_id="palmimo-042", initial_password="sticker-password")
    setup_password(adapters.state, "owner-password")
    before = adapters.state.read_auth()
    assert before is not None
    headers = {"X-Requested-With": "PalmimoPortal", "X-Forwarded-For": "192.0.2.50"}
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = listener.getsockname()[1]
        server = uvicorn.Server(config)
        thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
        with caplog.at_level(logging.WARNING, logger="palmimo_portal"):
            thread.start()
            try:
                deadline = time.monotonic() + 10
                while not server.started:
                    if not thread.is_alive() or time.monotonic() > deadline:
                        pytest.fail("uvicorn did not start")
                    time.sleep(0.01)
                response = httpx.post(f"http://127.0.0.1:{port}/api/v1/auth/reset", headers=headers)
                assert response.status_code == 403
                assert response.json()["error"]["code"] == "reset_not_allowed_from_device"
                assert adapters.state.read_auth() == before
                logged_addresses = {
                    arg
                    for record in caplog.records
                    if record.name == "palmimo_portal" and record.levelno == logging.WARNING
                    if isinstance(record.args, tuple)
                    for arg in record.args
                    if arg in {"127.0.0.1", "192.0.2.50"}
                }
                assert logged_addresses == {"127.0.0.1"}
            finally:
                server.should_exit = True
                thread.join(timeout=10)
                assert not thread.is_alive()
    with TestClient(app, client=("192.0.2.20", 50000)) as remote_client:
        response = remote_client.post("/api/v1/auth/reset", headers=headers)
    assert response.status_code == 200
