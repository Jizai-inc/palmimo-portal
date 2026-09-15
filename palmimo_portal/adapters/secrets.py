"""Real :class:`~palmimo_portal.ports.SecretsStore`: JSON files under ``PALMIMO_SECRETS_DIR``.

Schema::

    <secrets_dir>/                 0700
      values.json                  {"schema": 1, "values": {NAME: {"value": ..., "updated_at": ...}}} (0600)
      bindings.json                {"schema": 1, "bindings": {app: {REQ: STORE}}} (0600)
      git_credentials.json         {"schema": 1, "credentials": {host_owner: {"token": ..., "updated_at": ...}}} (0600)

Every write goes through :func:`~palmimo_portal.adapters.atomic_write.atomic_write_text` --
same durability contract as ``auth.json`` (see that module's docstring).
A corrupt/unparseable file raises rather than silently reading as empty:
unlike the app ledger, there is no separate "state file state" the API
needs to distinguish for secrets, so a read failure here is a genuine bug
surfaced as a 500, not a designed-for "corrupt" UI state.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from palmimo_portal.adapters.atomic_write import atomic_write_text, ensure_private_dir
from palmimo_portal.core.secrets import normalize_host_owner, validate_secret_name, validate_secret_value
from palmimo_portal.ports import (
    GitCredentialRecord,
    SecretInUseError,
    SecretNotFoundError,
    SecretRecord,
    SecretsStore,
    UnknownSecretNameError,
)


_SCHEMA_VERSION = 1
VALUES_FILENAME = "values.json"
BINDINGS_FILENAME = "bindings.json"
GIT_CREDENTIALS_FILENAME = "git_credentials.json"


class JsonSecretsStore(SecretsStore):
    def __init__(self, secrets_dir: Path) -> None:
        self._dir = secrets_dir

    @property
    def _values_path(self) -> Path:
        return self._dir / VALUES_FILENAME

    @property
    def _bindings_path(self) -> Path:
        return self._dir / BINDINGS_FILENAME

    @property
    def _git_credentials_path(self) -> Path:
        return self._dir / GIT_CREDENTIALS_FILENAME

    def _read_json(self, path: Path, *, key: str) -> dict[str, Any]:
        ensure_private_dir(self._dir)
        if not path.is_file():
            return {}
        data: Any = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or data.get("schema") != _SCHEMA_VERSION:
            raise ValueError(f"{path} has an unrecognized schema")
        payload = data.get(key, {})
        if not isinstance(payload, dict):
            raise ValueError(f"{path} field {key!r} must be an object")
        return payload

    def _write_json(self, path: Path, *, key: str, payload: dict[str, Any]) -> None:
        atomic_write_text(path, json.dumps({"schema": _SCHEMA_VERSION, key: payload}))

    def list_secrets(self) -> list[SecretRecord]:
        values = self._read_json(self._values_path, key="values")
        return [SecretRecord(name=name, updated_at=entry["updated_at"]) for name, entry in values.items()]

    def set_secret(self, name: str, value: str) -> None:
        validate_secret_name(name)
        validate_secret_value(value)
        values = self._read_json(self._values_path, key="values")
        values[name] = {"value": value, "updated_at": time.time()}
        self._write_json(self._values_path, key="values", payload=values)

    def get_secret_value(self, name: str) -> str | None:
        values = self._read_json(self._values_path, key="values")
        entry = values.get(name)
        return entry["value"] if entry is not None else None

    def delete_secret(self, name: str) -> None:
        values = self._read_json(self._values_path, key="values")
        if name not in values:
            raise SecretNotFoundError(name)
        users = self.bindings_using(name)
        if users:
            raise SecretInUseError(users)
        del values[name]
        self._write_json(self._values_path, key="values", payload=values)

    def read_bindings(self, app: str) -> dict[str, str]:
        bindings = self._read_json(self._bindings_path, key="bindings")
        return dict(bindings.get(app, {}))

    def write_bindings(self, app: str, bindings: dict[str, str]) -> None:
        values = self._read_json(self._values_path, key="values")
        unknown = sorted(set(bindings.values()) - set(values))
        if unknown:
            raise UnknownSecretNameError(f"unregistered secret name(s): {unknown}")
        all_bindings = self._read_json(self._bindings_path, key="bindings")
        all_bindings[app] = dict(bindings)
        self._write_json(self._bindings_path, key="bindings", payload=all_bindings)

    def delete_bindings(self, app: str) -> None:
        all_bindings = self._read_json(self._bindings_path, key="bindings")
        if app in all_bindings:
            del all_bindings[app]
            self._write_json(self._bindings_path, key="bindings", payload=all_bindings)

    def bindings_using(self, secret_name: str) -> list[tuple[str, str]]:
        all_bindings = self._read_json(self._bindings_path, key="bindings")
        return [
            (app, request_name)
            for app, app_bindings in all_bindings.items()
            for request_name, store_name in app_bindings.items()
            if store_name == secret_name
        ]

    def list_git_credentials(self) -> list[GitCredentialRecord]:
        credentials = self._read_json(self._git_credentials_path, key="credentials")
        return [
            GitCredentialRecord(
                host_owner=host_owner,
                updated_at=entry["updated_at"],
                rejected_at=entry.get("rejected_at"),
                rejected_status=entry.get("rejected_status"),
            )
            for host_owner, entry in credentials.items()
        ]

    def set_git_credential(self, host_owner: str, token: str) -> None:
        host_owner = normalize_host_owner(host_owner)
        validate_secret_value(token)
        credentials = self._read_json(self._git_credentials_path, key="credentials")
        credentials[host_owner] = {"token": token, "updated_at": time.time()}
        self._write_json(self._git_credentials_path, key="credentials", payload=credentials)

    def mark_git_credential_rejected(self, host_owner: str, status: int) -> None:
        host_owner = normalize_host_owner(host_owner)
        credentials = self._read_json(self._git_credentials_path, key="credentials")
        entry = credentials.get(host_owner)
        if entry is None:
            return
        entry["rejected_at"] = time.time()
        entry["rejected_status"] = status
        self._write_json(self._git_credentials_path, key="credentials", payload=credentials)

    def get_git_credential(self, host_owner: str) -> str | None:
        host_owner = normalize_host_owner(host_owner)
        credentials = self._read_json(self._git_credentials_path, key="credentials")
        entry = credentials.get(host_owner)
        return entry["token"] if entry is not None else None

    def delete_git_credential(self, host_owner: str) -> None:
        host_owner = normalize_host_owner(host_owner)
        credentials = self._read_json(self._git_credentials_path, key="credentials")
        if host_owner in credentials:
            del credentials[host_owner]
            self._write_json(self._git_credentials_path, key="credentials", payload=credentials)

    def reset(self) -> None:
        for path in (self._values_path, self._bindings_path, self._git_credentials_path):
            path.unlink(missing_ok=True)
