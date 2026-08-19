"""Tests for :mod:`palmimo_portal.__main__`'s logging setup."""

from __future__ import annotations

import logging
from collections.abc import Iterator

import pytest

from palmimo_portal.__main__ import _configure_logging


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
