"""Git credential scopes normalize GitHub owners but preserve other hosts."""

from __future__ import annotations

from palmimo_portal.core.apps import host_owner_from_url
from palmimo_portal.core.secrets import normalize_host_owner
from palmimo_portal.testing.fakes import FakeSecretsStore


def test_github_owner_matches_url_and_legacy_mixed_case_storage() -> None:
    store = FakeSecretsStore()
    store._git_credentials["github.com/Jizai-inc"] = ("token", 1.0)

    assert host_owner_from_url("https://github.com/jizai-inc/repo") == "github.com/jizai-inc"
    assert store.get_git_credential("github.com/jizai-inc") == "token"


def test_non_github_owner_preserves_case() -> None:
    assert normalize_host_owner("git.example.com/Acme") != normalize_host_owner("git.example.com/acme")
