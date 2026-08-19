"""Shared fixtures for the Palmimo Portal test suite.

Every test drives the app through fakes only — no filesystem, network, or
D-Bus access happens here. ``allowed_hosts`` includes ``testserver``, the
default ``Host`` header Starlette's ``TestClient`` sends, so HostGuard does
not need to be worked around in every unrelated test.

The ``live`` marker is the one exception: it is for
``tests/test_comitup_live.py``, which talks to a real comitup D-Bus service
and only makes sense on a Pi actually running comitup. It is registered in
the root ``pyproject.toml`` (so ``pytest --markers``/strict-marker checking
sees it everywhere), skipped by default here, and only runs when ``--live``
is passed on the command line -- CI never passes it, so those tests never
run there.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from palmimo_portal.api.app import create_app
from palmimo_portal.settings import Settings
from palmimo_portal.testing.fakes import FakeAdapterBundle


CSRF_HEADERS = {"X-Requested-With": "PalmimoPortal"}


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption(
        "--live",
        action="store_true",
        default=False,
        help="run tests marked 'live' (require a real comitup D-Bus service; Pi-only)",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    if config.getoption("--live"):
        return
    skip_live = pytest.mark.skip(reason="needs --live (requires a real comitup D-Bus service)")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip_live)


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # static_dir points at a directory that does not exist, deliberately:
    # most of this suite is unrelated to the frontend, and letting it default
    # to the real palmimo_portal/static/ would make those tests'
    # behavior depend on whether `make build` happens to have been run on
    # the machine running them (see test_app.py's SPA-fallback tests, which
    # override this on purpose). See Settings.static_dir's docstring.
    return Settings(allowed_hosts=frozenset({"testserver"}), static_dir=tmp_path / "static-not-built")


@pytest.fixture
def app(settings: Settings) -> FastAPI:
    return create_app(settings)


@pytest.fixture
def adapters(app: FastAPI) -> FakeAdapterBundle:
    # `settings` (above) never overrides `adapters`, so this is always the
    # "fake" default (see Settings.adapters) -- the cast makes that true
    # for the type checker too, so tests can reach fake-only attributes
    # (adapters.network.known_networks, adapters.updater.fail_at_step, ...)
    # without every one of them re-asserting isinstance(). See
    # FakeAdapterBundle's own docstring for why AdapterBundle itself can't
    # be typed this narrowly.
    return cast(FakeAdapterBundle, app.state.adapters)


@pytest.fixture
def client(app: FastAPI) -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
