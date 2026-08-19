"""Tests for :mod:`palmimo_portal.settings`."""

from __future__ import annotations

from pathlib import Path

import pytest

from palmimo_portal.settings import DEFAULT_PORT, get_settings


def test_get_settings_defaults_port_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALMIMO_PORT", raising=False)

    settings = get_settings()

    assert settings.port == DEFAULT_PORT


def test_get_settings_rejects_a_malformed_port_naming_the_variable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALMIMO_PORT", "not-a-number")

    with pytest.raises(ValueError, match="PALMIMO_PORT"):
        get_settings()


def test_get_settings_enable_docs_defaults_to_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALMIMO_ENABLE_DOCS", raising=False)

    settings = get_settings()

    assert settings.enable_docs is False


@pytest.mark.parametrize("value", ["1", "true", "True", "yes", "on"])
def test_get_settings_enable_docs_true_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PALMIMO_ENABLE_DOCS", value)

    settings = get_settings()

    assert settings.enable_docs is True


@pytest.mark.parametrize("value", ["0", "false", "False", "no", "off", ""])
def test_get_settings_enable_docs_false_values(monkeypatch: pytest.MonkeyPatch, value: str) -> None:
    monkeypatch.setenv("PALMIMO_ENABLE_DOCS", value)

    settings = get_settings()

    assert settings.enable_docs is False


def test_get_settings_defaults_update_repo_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALMIMO_UPDATE_REPO", raising=False)

    settings = get_settings()

    assert settings.update_repo == "Jizai-inc/palmimo-portal"


def test_get_settings_reads_update_repo_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALMIMO_UPDATE_REPO", "acme/other-repo")

    settings = get_settings()

    assert settings.update_repo == "acme/other-repo"


def test_get_settings_defaults_portal_unit_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALMIMO_PORTAL_UNIT", raising=False)

    settings = get_settings()

    assert settings.portal_unit == "palmimo-portal.service"


def test_get_settings_reads_portal_unit_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALMIMO_PORTAL_UNIT", "custom.service")

    settings = get_settings()

    assert settings.portal_unit == "custom.service"


def test_get_settings_defaults_uv_bin_when_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PALMIMO_UV_BIN", raising=False)

    settings = get_settings()

    assert settings.uv_bin == "uv"


def test_get_settings_reads_uv_bin_from_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PALMIMO_UV_BIN", "/opt/uv/uv")

    settings = get_settings()

    assert settings.uv_bin == "/opt/uv/uv"


def test_get_settings_portal_dir_defaults_to_the_checkout_containing_this_package(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("PALMIMO_PORTAL_DIR", raising=False)

    settings = get_settings()

    assert (settings.portal_dir / "palmimo_portal").is_dir()


def test_get_settings_reads_portal_dir_from_the_environment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PALMIMO_PORTAL_DIR", str(tmp_path))

    settings = get_settings()

    assert settings.portal_dir == tmp_path
