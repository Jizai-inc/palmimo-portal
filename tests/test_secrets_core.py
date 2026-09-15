"""Behavioral tests for secret name/value validation and log masking (core/secrets.py)."""

from __future__ import annotations

import pytest

from palmimo_portal.core.secrets import (
    mask_authorization_lines,
    mask_git_env,
    validate_secret_name,
    validate_secret_value,
)
from palmimo_portal.ports import InvalidSecretNameError, InvalidSecretValueError


@pytest.mark.parametrize("name", ["OPENAI_API_KEY", "A", "A" * 64])
def test_validate_secret_name_accepts_well_formed_name(name: str) -> None:
    validate_secret_name(name)  # does not raise


@pytest.mark.parametrize("name", ["lower", "1LEADING", "HAS-DASH", "A" * 65, ""])
def test_validate_secret_name_rejects_malformed_name(name: str) -> None:
    with pytest.raises(InvalidSecretNameError):
        validate_secret_name(name)


def test_validate_secret_value_rejects_newline() -> None:
    with pytest.raises(InvalidSecretValueError):
        validate_secret_value("line1\nline2")


def test_validate_secret_value_accepts_ordinary_string() -> None:
    validate_secret_value("sk-abc123")  # does not raise


def test_mask_git_env_replaces_credential_value_only() -> None:
    env = {"GIT_CONFIG_VALUE_0": "Authorization: Basic secrettoken", "GIT_CONFIG_KEY_0": "http.extraheader"}
    masked = mask_git_env(env)
    assert "secrettoken" not in masked["GIT_CONFIG_VALUE_0"]
    assert masked["GIT_CONFIG_KEY_0"] == "http.extraheader"


def test_mask_authorization_lines_drops_lines_containing_token() -> None:
    stderr = "fatal: could not read\nAuthorization: Basic sekret\nremote: not found"
    masked = mask_authorization_lines(stderr)
    assert "sekret" not in masked
    assert "remote: not found" in masked
