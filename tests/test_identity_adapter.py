"""Tests for the real file-backed :class:`~palmimo_portal.ports.IdentityStore` adapter."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from palmimo_portal.adapters.identity import FileIdentityStore
from palmimo_portal.ports import IDENTITY_UNAVAILABLE, Identity


def test_read_identity_is_none_when_the_file_is_missing(tmp_path: Path) -> None:
    store = FileIdentityStore(tmp_path / "does-not-exist.json")

    assert store.read_identity() is None


def test_read_identity_parses_a_well_formed_file(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "argon2id$..."}))
    store = FileIdentityStore(path)

    assert store.read_identity() == Identity(device_id="palmimo-042", initial_password_hash="argon2id$...")


def test_read_identity_treats_malformed_json_as_absent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "identity.json"
    path.write_text("not valid json {{{")
    store = FileIdentityStore(path)

    with caplog.at_level(logging.ERROR):
        result = store.read_identity()

    assert result is None
    assert str(path) in caplog.text


def test_read_identity_treats_a_missing_field_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042"}))
    store = FileIdentityStore(path)

    assert store.read_identity() is None


def test_read_identity_treats_valid_json_of_the_wrong_top_level_type_as_absent(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    path.write_text("[]")
    store = FileIdentityStore(path)

    assert store.read_identity() is None


def test_read_identity_logs_the_error_exactly_once(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    path = tmp_path / "identity.json"
    path.write_text("garbage")
    store = FileIdentityStore(path)

    with caplog.at_level(logging.ERROR):
        store.read_identity()
        store.read_identity()
        store.read_identity()

    error_records = [record for record in caplog.records if record.levelno == logging.ERROR]
    assert len(error_records) == 1


def test_read_identity_is_cached_across_calls(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "hash"}))
    store = FileIdentityStore(path)
    first = store.read_identity()

    # Mutating the file after the first read must not change what a
    # manufacturing-written, read-once file reports for the rest of the
    # process's life.
    path.write_text(json.dumps({"device_id": "palmimo-999", "initial_password_hash": "other"}))

    assert store.read_identity() == first


def test_read_identity_re_reads_when_the_file_appears_after_a_clean_absence(tmp_path: Path) -> None:
    # /boot/firmware mounts separately from the Portal's own filesystem: if
    # the Portal starts before that mount is ready, the file looks
    # genuinely absent for a while. Caching that "absent" forever would
    # incorrectly and permanently classify a sticker/OEM device as
    # open_setup (claimable by anyone) even once the real file shows up.
    path = tmp_path / "identity.json"
    store = FileIdentityStore(path)
    assert store.read_identity() is None

    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "hash"}))

    assert store.read_identity() == Identity(device_id="palmimo-042", initial_password_hash="hash")


def test_read_identity_re_reads_repeatedly_while_the_file_stays_absent(tmp_path: Path) -> None:
    store = FileIdentityStore(tmp_path / "does-not-exist.json")

    assert store.read_identity() is None
    assert store.read_identity() is None
    assert store.read_identity() is None


def test_read_identity_reports_unavailable_on_a_transient_os_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "hash"}))
    store = FileIdentityStore(path)

    def broken_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        raise OSError("mount not ready")

    monkeypatch.setattr(Path, "read_text", broken_read_text)

    assert store.read_identity() is IDENTITY_UNAVAILABLE


def test_read_identity_unavailable_is_not_cached_and_re_reads_once_fixed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "hash"}))
    store = FileIdentityStore(path)
    real_read_text = Path.read_text
    broken = {"on": True}

    def flaky_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        if broken["on"]:
            raise OSError("mount not ready")
        return real_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", flaky_read_text)

    assert store.read_identity() is IDENTITY_UNAVAILABLE
    broken["on"] = False

    assert store.read_identity() == Identity(device_id="palmimo-042", initial_password_hash="hash")


def test_read_identity_logs_a_warning_on_a_transient_os_error_rate_limited_to_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "hash"}))
    store = FileIdentityStore(path)

    def broken_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        raise OSError("mount not ready")

    monkeypatch.setattr(Path, "read_text", broken_read_text)

    with caplog.at_level(logging.WARNING):
        store.read_identity()
        store.read_identity()
        store.read_identity()

    warning_records = [record for record in caplog.records if record.levelno == logging.WARNING]
    assert len(warning_records) == 1
    assert str(path) in warning_records[0].message


def test_read_identity_caches_a_successful_read_and_does_not_re_read_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "hash"}))
    store = FileIdentityStore(path)
    real_read_text = Path.read_text
    calls = {"n": 0}

    def counting_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        calls["n"] += 1
        return real_read_text(self, encoding=encoding, errors=errors)

    monkeypatch.setattr(Path, "read_text", counting_read_text)

    first = store.read_identity()
    store.read_identity()
    store.read_identity()

    assert first == Identity(device_id="palmimo-042", initial_password_hash="hash")
    assert calls["n"] == 1


def test_read_identity_uncached_bypasses_a_stale_cache(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "hash"}))
    store = FileIdentityStore(path)
    store.read_identity()  # prime the cache with a real Identity
    path.unlink()  # the file is gone, but the cache still holds the old Identity

    assert store.read_identity_uncached() is None
    # The uncached read must also have dropped the stale cache -- a later
    # cached read() must not keep serving the Identity from before the
    # file was removed.
    assert store.read_identity() is None


def test_read_identity_uncached_drops_the_cache_when_the_file_becomes_malformed(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "hash"}))
    store = FileIdentityStore(path)
    store.read_identity()  # prime the cache with a real Identity
    path.write_text("not valid json {{{")

    assert store.read_identity_uncached() is None
    assert store.read_identity() is None


def test_read_identity_uncached_refreshes_the_cache(tmp_path: Path) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "hash"}))
    store = FileIdentityStore(path)
    store.read_identity()  # prime the cache with the first identity
    path.write_text(json.dumps({"device_id": "palmimo-999", "initial_password_hash": "other"}))

    updated = store.read_identity_uncached()

    assert updated == Identity(device_id="palmimo-999", initial_password_hash="other")
    # A later read_identity() call must see the refreshed value, not the
    # identity that was cached before read_identity_uncached() ran.
    assert store.read_identity() == updated


def test_read_identity_uncached_never_caches_a_transient_unavailable_result(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "identity.json"
    path.write_text(json.dumps({"device_id": "palmimo-042", "initial_password_hash": "hash"}))
    store = FileIdentityStore(path)
    store.read_identity()  # prime the cache with a real Identity
    real_read_text = Path.read_text

    def broken_read_text(self: Path, encoding: str | None = None, errors: str | None = None) -> str:
        raise OSError("mount not ready")

    monkeypatch.setattr(Path, "read_text", broken_read_text)
    assert store.read_identity_uncached() is IDENTITY_UNAVAILABLE

    monkeypatch.setattr(Path, "read_text", real_read_text)

    # The transient failure above must not have clobbered the previously
    # cached Identity with something un-refreshable.
    assert store.read_identity() == Identity(device_id="palmimo-042", initial_password_hash="hash")
