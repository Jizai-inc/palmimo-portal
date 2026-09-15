"""Behavioral tests for SecretsStore, run against both the fake and the real JSON adapter.

Parametrized over both implementations so the fake cannot silently drift
from the contract the real adapter enforces.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from palmimo_portal.adapters.secrets import JsonSecretsStore
from palmimo_portal.ports import (
    SecretInUseError,
    SecretNotFoundError,
    SecretsStore,
    UnknownSecretNameError,
)
from palmimo_portal.testing.fakes import FakeSecretsStore


@pytest.fixture(params=["fake", "real"])
def store(request: pytest.FixtureRequest, tmp_path: Path) -> SecretsStore:
    if request.param == "fake":
        return FakeSecretsStore()
    return JsonSecretsStore(tmp_path / "secrets")


def test_list_secrets_never_exposes_value(store: SecretsStore) -> None:
    store.set_secret("OPENAI_API_KEY", "sk-super-secret")
    records = store.list_secrets()
    assert [r.name for r in records] == ["OPENAI_API_KEY"]
    assert not any("sk-super-secret" in repr(record) for record in records)


def test_delete_secret_in_use_is_refused_with_users(store: SecretsStore) -> None:
    store.set_secret("OPENAI_API_KEY", "sk-1")
    store.write_bindings("palmimo-teleop", {"API_KEY": "OPENAI_API_KEY"})

    with pytest.raises(SecretInUseError) as excinfo:
        store.delete_secret("OPENAI_API_KEY")
    assert excinfo.value.users == [("palmimo-teleop", "API_KEY")]


def test_delete_secret_not_found_raises() -> None:
    store = FakeSecretsStore()
    with pytest.raises(SecretNotFoundError):
        store.delete_secret("MISSING")


def test_write_bindings_to_unregistered_secret_name_raises(store: SecretsStore) -> None:
    with pytest.raises(UnknownSecretNameError):
        store.write_bindings("palmimo-teleop", {"API_KEY": "NOT_REGISTERED"})


def test_delete_secret_removes_it_once_unbound(store: SecretsStore) -> None:
    store.set_secret("OPENAI_API_KEY", "sk-1")
    store.write_bindings("palmimo-teleop", {"API_KEY": "OPENAI_API_KEY"})
    store.write_bindings("palmimo-teleop", {})

    store.delete_secret("OPENAI_API_KEY")

    assert store.list_secrets() == []


def test_delete_bindings_removes_every_binding_for_app(store: SecretsStore) -> None:
    store.set_secret("OPENAI_API_KEY", "sk-1")
    store.write_bindings("palmimo-teleop", {"API_KEY": "OPENAI_API_KEY"})

    store.delete_bindings("palmimo-teleop")

    assert store.read_bindings("palmimo-teleop") == {}
