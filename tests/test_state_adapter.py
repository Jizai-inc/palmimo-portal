"""Tests for the real JSON-file :class:`StateStore` adapter."""

from __future__ import annotations

import json
import logging
import os
import stat
import time
from pathlib import Path

import pytest

from palmimo_portal.adapters.state import (
    AUTH_FILENAME,
    LAST_ATTEMPT_FILENAME,
    UPDATE_STATE_FILENAME,
    JsonFileStateStore,
)
from palmimo_portal.core.update import IDLE_UPDATE_JOB, IDLE_UPDATE_STATE
from palmimo_portal.ports import (
    AuthAlreadyExistsError,
    AuthFileState,
    AuthState,
    Release,
    UpdateJob,
    UpdateState,
    WifiAttempt,
)


def test_read_auth_is_none_before_first_write(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    assert store.read_auth() is None


def test_write_then_read_auth_round_trips(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = AuthState(password_hash="hash", signing_key="key")

    store.write_auth(state)

    assert store.read_auth() == state


def test_auth_file_is_written_with_mode_0600(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    store.write_auth(AuthState(password_hash="hash", signing_key="key"))

    mode = stat.S_IMODE((tmp_path / AUTH_FILENAME).stat().st_mode)
    assert mode == 0o600


def test_auth_state_survives_a_reload_from_a_fresh_store_instance(tmp_path: Path) -> None:
    JsonFileStateStore(tmp_path).write_auth(AuthState(password_hash="hash", signing_key="key"))

    reloaded = JsonFileStateStore(tmp_path).read_auth()

    assert reloaded == AuthState(password_hash="hash", signing_key="key")


def test_read_last_wifi_attempt_is_none_before_first_write(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    assert store.read_last_wifi_attempt() is None


def test_write_then_read_last_wifi_attempt_round_trips(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    attempt = WifiAttempt(ssid="home", result="failed", timestamp=123.5)

    store.write_last_wifi_attempt(attempt)

    assert store.read_last_wifi_attempt() == attempt


def test_writing_a_second_wifi_attempt_replaces_the_first(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    store.write_last_wifi_attempt(WifiAttempt(ssid="home", result="failed", timestamp=1.0))

    store.write_last_wifi_attempt(WifiAttempt(ssid="home", result="success", timestamp=2.0))

    assert store.read_last_wifi_attempt() == WifiAttempt(ssid="home", result="success", timestamp=2.0)


def test_write_creates_the_state_directory_if_missing(tmp_path: Path) -> None:
    state_dir = tmp_path / "nested" / "portal"
    store = JsonFileStateStore(state_dir)

    store.write_auth(AuthState(password_hash="hash", signing_key="key"))

    assert state_dir.is_dir()


def test_write_auth_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    store.write_auth(AuthState(password_hash="hash", signing_key="key"))

    assert {p.name for p in tmp_path.iterdir()} == {AUTH_FILENAME}


def test_write_last_wifi_attempt_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    store.write_last_wifi_attempt(WifiAttempt(ssid="home", result="failed", timestamp=1.0))

    assert {p.name for p in tmp_path.iterdir()} == {LAST_ATTEMPT_FILENAME}


def test_read_auth_treats_a_corrupt_file_as_absent(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    auth_path = tmp_path / AUTH_FILENAME
    tmp_path.mkdir(parents=True, exist_ok=True)
    auth_path.write_text("not valid json {{{", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    with caplog.at_level(logging.ERROR):
        result = store.read_auth()

    assert result is None
    assert str(auth_path) in caplog.text


def test_read_auth_treats_a_file_missing_expected_keys_as_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    auth_path = tmp_path / AUTH_FILENAME
    auth_path.write_text('{"unexpected": "shape"}', encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    with caplog.at_level(logging.ERROR):
        result = store.read_auth()

    assert result is None
    assert str(auth_path) in caplog.text


def test_a_corrupt_auth_file_does_not_let_setup_password_silently_overwrite_it(tmp_path: Path) -> None:
    # This replaces the old self-heal contract: a corrupt auth.json used to
    # let setup_password() run again and overwrite it, which is exactly the
    # fail-open bug -- after a crash corrupts auth.json, anyone on the LAN
    # could claim an already-owned device. create_auth's O_CREAT|O_EXCL
    # semantics now fail closed even if a caller reaches setup_password
    # directly without going through the API layer's auth_state() gate.
    from palmimo_portal.core.auth import PasswordAlreadySetError, setup_password

    auth_path = tmp_path / AUTH_FILENAME
    auth_path.write_text("garbage", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    with pytest.raises(PasswordAlreadySetError):
        setup_password(store, "hunter2")

    assert auth_path.read_text(encoding="utf-8") == "garbage"


def test_read_last_wifi_attempt_treats_a_corrupt_file_as_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    attempt_path = tmp_path / LAST_ATTEMPT_FILENAME
    attempt_path.write_text("not valid json", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    with caplog.at_level(logging.ERROR):
        result = store.read_last_wifi_attempt()

    assert result is None
    assert str(attempt_path) in caplog.text


def test_read_last_wifi_attempt_treats_a_lone_surrogate_ssid_as_absent(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # A lone surrogate (e.g. "\ud800") is valid JSON to the stdlib decoder
    # but cannot encode to UTF-8 -- this is the poisoned-record scenario
    # from a pre-validation `POST /wifi/connect` that let one through: the
    # file must self-heal to "absent" on read rather than raising past this
    # adapter's tolerant-read contract (which would otherwise 500 every
    # `GET /system/status` forever). Written as raw bytes since no Python
    # str containing a lone surrogate can be json.dumps'd.
    attempt_path = tmp_path / LAST_ATTEMPT_FILENAME
    attempt_path.write_bytes(b'{"ssid": "\\ud800", "result": "attempting", "timestamp": 1.0}')
    store = JsonFileStateStore(tmp_path)

    with caplog.at_level(logging.WARNING):
        result = store.read_last_wifi_attempt()

    assert result is None
    assert str(attempt_path) in caplog.text


def test_read_last_wifi_attempt_deletes_a_poisoned_file_instead_of_re_warning_on_every_poll(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Without deleting the file, `GET /system/status`'s ~10s poll would
    # re-warn forever -- thousands of journal lines a day for a device that
    # never recovers on its own. The read path must heal by removing the
    # poisoned file, not merely by masking it on every call.
    attempt_path = tmp_path / LAST_ATTEMPT_FILENAME
    attempt_path.write_bytes(b'{"ssid": "\\ud800", "result": "attempting", "timestamp": 1.0}')
    store = JsonFileStateStore(tmp_path)

    with caplog.at_level(logging.WARNING):
        first = store.read_last_wifi_attempt()

    assert first is None
    assert not attempt_path.exists()

    # A second read, with the file already gone, must not warn again --
    # confirms the deletion actually happened rather than the file being
    # transiently missing for some other reason.
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        second = store.read_last_wifi_attempt()

    assert second is None
    assert caplog.text == ""


def test_read_last_wifi_attempt_survives_the_file_already_being_gone_when_it_tries_to_delete(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A concurrent deleter (another read racing the same poisoned file, or
    # an operator deleting it over SSH) can win between this read's
    # is_file() check and its own unlink -- that race must not raise past
    # the tolerant-read contract.
    attempt_path = tmp_path / LAST_ATTEMPT_FILENAME
    attempt_path.write_bytes(b'{"ssid": "\\ud800", "result": "attempting", "timestamp": 1.0}')
    store = JsonFileStateStore(tmp_path)

    real_unlink = Path.unlink

    def racing_unlink(self: Path, *, missing_ok: bool = False) -> None:
        # Simulate another process winning the unlink race: the file is
        # already gone by the time this call happens.
        if self == attempt_path:
            real_unlink(self, missing_ok=True)
        real_unlink(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", racing_unlink)

    result = store.read_last_wifi_attempt()

    assert result is None


def test_preflight_state_dir_creates_a_private_directory(tmp_path: Path) -> None:
    from palmimo_portal.adapters.state import preflight_state_dir

    state_dir = tmp_path / "nested" / "portal"

    preflight_state_dir(state_dir)

    assert state_dir.is_dir()
    assert stat.S_IMODE(state_dir.stat().st_mode) == 0o700


def test_preflight_state_dir_probe_writes_and_cleans_up(tmp_path: Path) -> None:
    from palmimo_portal.adapters.state import preflight_state_dir

    state_dir = tmp_path / "portal"

    preflight_state_dir(state_dir)

    assert list(state_dir.iterdir()) == []


def test_preflight_state_dir_sweeps_an_old_leftover_atomic_write_temp_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from palmimo_portal.adapters.state import AUTH_FILENAME, preflight_state_dir

    # atomic_write_text's mkstemp pattern: ".<name>.<random>.tmp" -- an
    # orphan of exactly this shape is what a crash between mkstemp and the
    # rename onto the final path would leave behind. Back-dated past the
    # sweep's minimum age (see _ORPHAN_TEMP_MIN_AGE_SECONDS) so it reads as
    # genuinely orphaned, not as a write that might still be in flight.
    state_dir = tmp_path / "portal"
    state_dir.mkdir(mode=0o700)
    orphan = state_dir / f".{AUTH_FILENAME}.abc123.tmp"
    orphan.write_text("stale partial write", encoding="utf-8")
    old = time.time() - 3600
    os.utime(orphan, (old, old))

    with caplog.at_level(logging.INFO):
        preflight_state_dir(state_dir)

    assert not orphan.exists()
    assert str(orphan) in caplog.text


def test_preflight_state_dir_does_not_sweep_a_fresh_temp_file(tmp_path: Path) -> None:
    """A temp file young enough to still be an in-flight write must survive the sweep.

    Protects a concurrent process's atomic write (mkstemp done, rename not
    yet run) from losing its temp file to a startup sweep racing it -- see
    ``_sweep_orphan_temp_files``'s docstring for the single-instance
    assumption this still relies on.
    """
    from palmimo_portal.adapters.state import AUTH_FILENAME, preflight_state_dir

    state_dir = tmp_path / "portal"
    state_dir.mkdir(mode=0o700)
    fresh = state_dir / f".{AUTH_FILENAME}.abc123.tmp"
    fresh.write_text("write in flight", encoding="utf-8")

    preflight_state_dir(state_dir)

    assert fresh.exists()


def test_preflight_state_dir_does_not_sweep_real_state_files(tmp_path: Path) -> None:
    from palmimo_portal.adapters.state import AUTH_FILENAME, preflight_state_dir

    state_dir = tmp_path / "portal"
    state_dir.mkdir(mode=0o700)
    real_file = state_dir / AUTH_FILENAME
    real_file.write_text('{"password_hash": "x", "signing_key": "y"}', encoding="utf-8")

    preflight_state_dir(state_dir)

    assert real_file.exists()


def test_preflight_state_dir_raises_a_clear_error_when_unwritable(tmp_path: Path) -> None:
    from palmimo_portal.adapters.state import preflight_state_dir

    blocked = tmp_path / "blocked"
    blocked.mkdir(mode=0o500)
    state_dir = blocked / "portal"

    try:
        with pytest.raises(RuntimeError, match=str(state_dir)):
            preflight_state_dir(state_dir)
    finally:
        blocked.chmod(0o700)


def test_auth_state_is_absent_before_first_write(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    assert store.auth_state() is AuthFileState.ABSENT


def test_auth_state_is_present_after_a_normal_write(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    store.write_auth(AuthState(password_hash="hash", signing_key="key"))

    assert store.auth_state() is AuthFileState.PRESENT


def test_auth_state_is_corrupt_for_unparseable_json(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    (tmp_path / AUTH_FILENAME).write_text("not valid json {{{", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    with caplog.at_level(logging.ERROR):
        state = store.auth_state()

    assert state is AuthFileState.CORRUPT
    assert str(tmp_path / AUTH_FILENAME) in caplog.text


def test_auth_state_is_corrupt_for_a_missing_field(tmp_path: Path) -> None:
    (tmp_path / AUTH_FILENAME).write_text('{"unexpected": "shape"}', encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    assert store.auth_state() is AuthFileState.CORRUPT


def test_auth_state_is_corrupt_for_valid_json_of_the_wrong_top_level_type(tmp_path: Path) -> None:
    # "[]" is valid JSON but not the expected object shape -- a naive
    # data["password_hash"] would raise TypeError, not one of the
    # originally-caught (JSONDecodeError, KeyError, OSError).
    (tmp_path / AUTH_FILENAME).write_text("[]", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    assert store.auth_state() is AuthFileState.CORRUPT


def test_read_auth_is_none_when_the_file_is_corrupt(tmp_path: Path) -> None:
    (tmp_path / AUTH_FILENAME).write_text("garbage", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    assert store.read_auth() is None


def test_auth_state_returns_to_absent_after_the_corrupt_file_is_deleted(tmp_path: Path) -> None:
    auth_path = tmp_path / AUTH_FILENAME
    auth_path.write_text("garbage", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)
    assert store.auth_state() is AuthFileState.CORRUPT

    auth_path.unlink()

    assert store.auth_state() is AuthFileState.ABSENT


def test_delete_auth_removes_a_present_auth_file(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    store.write_auth(AuthState(password_hash="hash", signing_key="key"))

    store.delete_auth()

    assert store.auth_state() is AuthFileState.ABSENT
    assert store.read_auth() is None


def test_delete_auth_is_a_no_op_when_the_file_is_already_missing(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    store.delete_auth()  # must not raise

    assert store.auth_state() is AuthFileState.ABSENT


def test_delete_auth_removes_a_corrupt_auth_file_too(tmp_path: Path) -> None:
    auth_path = tmp_path / AUTH_FILENAME
    auth_path.write_text("not valid json {{{", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)
    assert store.auth_state() is AuthFileState.CORRUPT

    store.delete_auth()

    assert store.auth_state() is AuthFileState.ABSENT


def test_delete_auth_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    # lock_auth()'s own lockfile (AUTH_LOCK_FILENAME) is expected to remain
    # -- it is a persistent artifact of locking, not a mkstemp orphan -- but
    # no ".*.tmp" leftover from delete_auth's own work must survive.
    store = JsonFileStateStore(tmp_path)
    store.write_auth(AuthState(password_hash="hash", signing_key="key"))

    store.delete_auth()

    remaining = {p.name for p in tmp_path.iterdir()}
    assert AUTH_FILENAME not in remaining
    assert not any(name.endswith(".tmp") for name in remaining)


def test_delete_auth_rotates_an_existing_initial_signing_key(tmp_path: Path) -> None:
    from palmimo_portal.adapters.state import INITIAL_SESSION_KEY_FILENAME

    store = JsonFileStateStore(tmp_path)
    store.write_auth(AuthState(password_hash="hash", signing_key="key"))
    old_initial_key = store.read_or_create_initial_signing_key()

    store.delete_auth()

    assert (tmp_path / INITIAL_SESSION_KEY_FILENAME).exists()
    new_initial_key = store.read_or_create_initial_signing_key()
    assert new_initial_key != old_initial_key


def test_delete_auth_does_not_eagerly_create_an_initial_signing_key(tmp_path: Path) -> None:
    from palmimo_portal.adapters.state import INITIAL_SESSION_KEY_FILENAME

    store = JsonFileStateStore(tmp_path)
    store.write_auth(AuthState(password_hash="hash", signing_key="key"))

    store.delete_auth()

    assert not (tmp_path / INITIAL_SESSION_KEY_FILENAME).exists()


def test_delete_auth_leaves_auth_json_intact_when_the_key_rotation_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The initial-mode key is rotated *before* auth.json is unlinked -- a failure rotating it
    must leave auth.json (and the old key) untouched, not deleted credentials plus a stale
    initial-mode cookie that still verifies.
    """
    import palmimo_portal.adapters.state as state_module

    store = JsonFileStateStore(tmp_path)
    store.write_auth(AuthState(password_hash="hash", signing_key="key"))
    old_initial_key = store.read_or_create_initial_signing_key()

    def failing_write(path: Path, text: str) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr(state_module, "atomic_write_text", failing_write)

    with pytest.raises(OSError):
        store.delete_auth()

    monkeypatch.undo()
    assert store.auth_state() is AuthFileState.PRESENT
    assert store.read_or_create_initial_signing_key() == old_initial_key


def test_create_auth_creates_the_file(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = AuthState(password_hash="hash", signing_key="key")

    store.create_auth(state)

    assert store.read_auth() == state
    assert store.auth_state() is AuthFileState.PRESENT


def test_create_auth_raises_when_auth_already_exists(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    store.create_auth(AuthState(password_hash="first", signing_key="key-1"))

    with pytest.raises(AuthAlreadyExistsError):
        store.create_auth(AuthState(password_hash="second", signing_key="key-2"))


def test_create_auth_the_race_loser_does_not_clobber_the_winner(tmp_path: Path) -> None:
    # Two JsonFileStateStore instances pointed at the same directory model
    # two concurrent /setup requests -- the second call must lose cleanly,
    # not silently overwrite the first (the read-then-write race this
    # replaces would let both "succeed", last write wins).
    store_a = JsonFileStateStore(tmp_path)
    store_b = JsonFileStateStore(tmp_path)
    winner = AuthState(password_hash="winner", signing_key="winner-key")

    store_a.create_auth(winner)
    with pytest.raises(AuthAlreadyExistsError):
        store_b.create_auth(AuthState(password_hash="loser", signing_key="loser-key"))

    assert store_a.read_auth() == winner
    assert store_b.read_auth() == winner


def test_setup_password_race_the_loser_gets_password_already_set_error(tmp_path: Path) -> None:
    from palmimo_portal.core.auth import PasswordAlreadySetError, setup_password

    store = JsonFileStateStore(tmp_path)
    original_create_auth = store.create_auth

    def racing_create_auth(state: AuthState) -> None:
        # Simulate a concurrent winner sneaking in between setup_password's
        # decision to create auth material and the actual filesystem call.
        JsonFileStateStore(tmp_path).create_auth(AuthState(password_hash="other", signing_key="other-key"))
        original_create_auth(state)

    store.create_auth = racing_create_auth  # type: ignore[method-assign]  # deliberate monkeypatch of a bound method for this test

    with pytest.raises(PasswordAlreadySetError):
        setup_password(store, "hunter2")

    assert store.read_auth() == AuthState(password_hash="other", signing_key="other-key")


def test_read_last_wifi_attempt_treats_valid_json_of_the_wrong_type_as_absent(tmp_path: Path) -> None:
    (tmp_path / LAST_ATTEMPT_FILENAME).write_text("[]", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    assert store.read_last_wifi_attempt() is None


def test_read_or_create_initial_signing_key_creates_a_key_on_first_call(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    key = store.read_or_create_initial_signing_key()

    assert key
    assert isinstance(key, str)


def test_read_or_create_initial_signing_key_is_stable_within_an_instance(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    first = store.read_or_create_initial_signing_key()
    second = store.read_or_create_initial_signing_key()

    assert first == second


def test_read_or_create_initial_signing_key_survives_a_reload_from_a_fresh_instance(tmp_path: Path) -> None:
    created = JsonFileStateStore(tmp_path).read_or_create_initial_signing_key()

    reloaded = JsonFileStateStore(tmp_path).read_or_create_initial_signing_key()

    assert reloaded == created


def test_read_or_create_initial_signing_key_is_written_with_mode_0600(tmp_path: Path) -> None:
    from palmimo_portal.adapters.state import INITIAL_SESSION_KEY_FILENAME

    store = JsonFileStateStore(tmp_path)
    store.read_or_create_initial_signing_key()

    mode = stat.S_IMODE((tmp_path / INITIAL_SESSION_KEY_FILENAME).stat().st_mode)
    assert mode == 0o600


def test_read_or_create_initial_signing_key_race_loser_reads_back_the_winner(tmp_path: Path) -> None:
    # Two JsonFileStateStore instances pointed at the same directory model
    # two concurrent /auth/login requests both hitting an empty state dir
    # -- they must end up agreeing on one key, not each signing tokens
    # under a different one.
    store_a = JsonFileStateStore(tmp_path)
    store_b = JsonFileStateStore(tmp_path)

    key_a = store_a.read_or_create_initial_signing_key()
    key_b = store_b.read_or_create_initial_signing_key()

    assert key_a == key_b


def test_discard_initial_signing_key_removes_the_file(tmp_path: Path) -> None:
    from palmimo_portal.adapters.state import INITIAL_SESSION_KEY_FILENAME

    store = JsonFileStateStore(tmp_path)
    store.read_or_create_initial_signing_key()
    assert (tmp_path / INITIAL_SESSION_KEY_FILENAME).exists()

    store.discard_initial_signing_key()

    assert not (tmp_path / INITIAL_SESSION_KEY_FILENAME).exists()


def test_discard_initial_signing_key_is_a_no_op_when_no_key_exists(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    store.discard_initial_signing_key()  # must not raise


def test_discard_initial_signing_key_fsyncs_the_parent_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import os

    # atomic_write_text already fsyncs the parent directory after a create,
    # so a crash right after that call cannot lose the directory entry that
    # points at the new file. discard_initial_signing_key must give the same
    # guarantee in the other direction: a crash right after this call
    # returns must not resurrect the unlinked file by losing the directory
    # entry's removal instead.
    store = JsonFileStateStore(tmp_path)
    store.read_or_create_initial_signing_key()

    fsynced_dir_fds: list[int] = []
    original_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        fsynced_dir_fds.append(fd)
        original_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)

    store.discard_initial_signing_key()

    assert len(fsynced_dir_fds) == 1


def test_change_password_from_initial_discards_the_initial_signing_key(tmp_path: Path) -> None:
    from palmimo_portal.adapters.state import INITIAL_SESSION_KEY_FILENAME
    from palmimo_portal.core.auth import change_password_from_initial

    store = JsonFileStateStore(tmp_path)
    store.read_or_create_initial_signing_key()
    assert (tmp_path / INITIAL_SESSION_KEY_FILENAME).exists()

    change_password_from_initial(store, "new-owner-password")

    assert not (tmp_path / INITIAL_SESSION_KEY_FILENAME).exists()


def test_read_or_create_initial_signing_key_does_not_resurrect_a_key_deleted_out_from_under_it(
    tmp_path: Path,
) -> None:
    # A StateStore instance that already returned a key once must not keep
    # serving it from an in-memory cache after the file is deleted out from
    # under it (e.g. a factory reset with no process restart) -- the next
    # read must genuinely go back to disk and mint a fresh key.
    from palmimo_portal.adapters.state import INITIAL_SESSION_KEY_FILENAME

    store = JsonFileStateStore(tmp_path)
    first_key = store.read_or_create_initial_signing_key()

    (tmp_path / INITIAL_SESSION_KEY_FILENAME).unlink()

    second_key = store.read_or_create_initial_signing_key()

    assert second_key != first_key


def test_read_or_create_initial_signing_key_repairs_a_corrupt_file(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from palmimo_portal.adapters.state import INITIAL_SESSION_KEY_FILENAME

    key_path = tmp_path / INITIAL_SESSION_KEY_FILENAME
    key_path.write_text("not valid json {{{", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    with caplog.at_level(logging.WARNING):
        key = store.read_or_create_initial_signing_key()

    assert key
    assert any(record.levelno == logging.WARNING for record in caplog.records)
    # The repair is persisted, not just returned in memory.
    assert json.loads(key_path.read_text(encoding="utf-8"))["signing_key"] == key


def test_read_or_create_initial_signing_key_repair_is_stable_across_reads(tmp_path: Path) -> None:
    from palmimo_portal.adapters.state import INITIAL_SESSION_KEY_FILENAME

    key_path = tmp_path / INITIAL_SESSION_KEY_FILENAME
    key_path.write_text("garbage", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    first_repair = store.read_or_create_initial_signing_key()
    second_read = store.read_or_create_initial_signing_key()
    reloaded = JsonFileStateStore(tmp_path).read_or_create_initial_signing_key()

    assert first_repair == second_read == reloaded


def test_read_or_create_initial_signing_key_true_race_loser_adopts_the_winners_key(tmp_path: Path) -> None:
    # Simulates a concurrent winner sneaking in between this store's
    # "no file yet" read and its own attempt to create one -- the same
    # pattern test_setup_password_race_the_loser_gets_password_already_set_error
    # uses for create_auth. Forces the loser's initial read to report
    # "absent" (as it genuinely would, mid-race) even though a winner has
    # since created the file, so its create attempt hits FileExistsError
    # for real and must recover by reading the winner's key back.
    store = JsonFileStateStore(tmp_path)
    original_read = store._read_initial_signing_key
    calls = {"n": 0}

    def racing_read() -> str | None:
        calls["n"] += 1
        if calls["n"] == 1:
            JsonFileStateStore(tmp_path).read_or_create_initial_signing_key()  # the "winner"
            return None
        return original_read()

    store._read_initial_signing_key = racing_read  # type: ignore[method-assign]  # deliberate monkeypatch of a bound method for this test

    loser_key = store.read_or_create_initial_signing_key()
    winner_key = JsonFileStateStore(tmp_path).read_or_create_initial_signing_key()

    assert loser_key == winner_key


def test_lock_auth_is_reentrant_across_sequential_uses(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    with store.lock_auth():
        pass
    with store.lock_auth():
        pass  # must not raise or deadlock


def test_lock_auth_blocks_a_concurrent_holder(tmp_path: Path) -> None:
    import threading

    # Two separate JsonFileStateStore instances over the same state_dir --
    # the real-world shape (two request-handling threads, each with its own
    # StateStore instance from dependency injection) -- must still
    # serialize via the shared lockfile on disk, not just within one
    # instance's own Python-level state.
    store_a = JsonFileStateStore(tmp_path)
    store_b = JsonFileStateStore(tmp_path)

    entered_second = threading.Event()

    def second_holder() -> None:
        with store_b.lock_auth():
            entered_second.set()

    with store_a.lock_auth():
        thread = threading.Thread(target=second_holder)
        thread.start()
        # The second holder must not acquire the lock while store_a holds it.
        assert not entered_second.wait(timeout=0.2)

    # Released now -- the second holder can proceed.
    assert entered_second.wait(timeout=5.0)
    thread.join(timeout=5.0)


def test_lock_auth_raises_after_the_bounded_timeout(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import threading

    from palmimo_portal.ports import AuthLockTimeoutError

    # A short timeout keeps this test fast without weakening what it
    # proves: a contender that cannot acquire the lock within the budget
    # must raise rather than block forever.
    monkeypatch.setattr("palmimo_portal.adapters.state.AUTH_LOCK_TIMEOUT_SECONDS", 0.2)
    store_a = JsonFileStateStore(tmp_path)
    store_b = JsonFileStateStore(tmp_path)
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store_a.lock_auth():
            holding.set()
            release.wait(timeout=5.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert holding.wait(timeout=2.0)

    with pytest.raises(AuthLockTimeoutError), store_b.lock_auth():
        pass  # pragma: no cover -- must never be entered

    release.set()
    thread.join(timeout=5.0)


def test_lock_auth_logs_a_warning_once_contention_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import threading

    from palmimo_portal.ports import AuthLockTimeoutError

    monkeypatch.setattr("palmimo_portal.adapters.state.AUTH_LOCK_TIMEOUT_SECONDS", 0.2)
    store_a = JsonFileStateStore(tmp_path)
    store_b = JsonFileStateStore(tmp_path)
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with store_a.lock_auth():
            holding.set()
            release.wait(timeout=5.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert holding.wait(timeout=2.0)

    with caplog.at_level(logging.WARNING), pytest.raises(AuthLockTimeoutError), store_b.lock_auth():
        pass  # pragma: no cover -- must never be entered

    release.set()
    thread.join(timeout=5.0)

    assert "auth lock contended" in caplog.text
    assert not thread.is_alive()


def test_read_update_state_is_idle_before_first_write(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    assert store.read_update_state() == IDLE_UPDATE_STATE


def test_write_then_read_update_state_round_trips(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = UpdateState(
        latest=Release(
            tag="v2.0.0", name="v2.0.0", published_at="2026-01-01T00:00:00Z", html_url="https://example.test"
        ),
        checked_at=100.0,
        previous_tag="v1.0.0",
        job=UpdateJob(
            state="running",
            kind="update",
            target="v2.0.0",
            step="checkout",
            error=None,
            started_at=100.0,
            finished_at=None,
        ),
    )

    store.write_update_state(state)

    assert store.read_update_state() == state


def test_write_update_state_round_trips_a_null_latest_and_job(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    store.write_update_state(IDLE_UPDATE_STATE)

    assert store.read_update_state() == IDLE_UPDATE_STATE


def test_update_state_survives_a_reload_from_a_fresh_store_instance(tmp_path: Path) -> None:
    state = UpdateState(
        latest=Release(
            tag="v2.0.0", name="v2.0.0", published_at="2026-01-01T00:00:00Z", html_url="https://example.test"
        ),
        checked_at=100.0,
        previous_tag=None,
        job=UpdateJob(
            state="idle", kind="update", target=None, step=None, error=None, started_at=None, finished_at=None
        ),
    )
    JsonFileStateStore(tmp_path).write_update_state(state)

    reloaded = JsonFileStateStore(tmp_path).read_update_state()

    assert reloaded == state


def test_read_update_state_treats_a_corrupt_file_as_idle(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    update_path = tmp_path / UPDATE_STATE_FILENAME
    update_path.parent.mkdir(parents=True, exist_ok=True)
    update_path.write_text("not valid json {{{", encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    with caplog.at_level(logging.WARNING):
        result = store.read_update_state()

    assert result == IDLE_UPDATE_STATE
    assert str(update_path) in caplog.text


def test_read_update_state_treats_a_file_missing_expected_keys_as_idle(tmp_path: Path) -> None:
    update_path = tmp_path / UPDATE_STATE_FILENAME
    update_path.write_text('{"unexpected": "shape"}', encoding="utf-8")
    store = JsonFileStateStore(tmp_path)

    assert store.read_update_state() == IDLE_UPDATE_STATE


def test_write_then_read_update_state_round_trips_restarting_at(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)
    state = UpdateState(
        latest=None,
        checked_at=100.0,
        previous_tag="v1.0.0",
        job=UpdateJob(
            state="restarting",
            kind="update",
            target="v2.0.0",
            step="checkout",
            error=None,
            started_at=100.0,
            finished_at=None,
            restarting_at=150.0,
        ),
    )

    store.write_update_state(state)

    assert store.read_update_state() == state


def test_read_update_state_tolerates_a_missing_restarting_at_field(tmp_path: Path) -> None:
    """update.json written before restarting_at existed -- parses fine, restarting_at is None."""
    update_path = tmp_path / UPDATE_STATE_FILENAME
    update_path.write_text(
        json.dumps(
            {
                "latest": None,
                "checked_at": None,
                "previous_tag": None,
                "job": {
                    "state": "restarting",
                    "kind": "update",
                    "target": "v2.0.0",
                    "step": "checkout",
                    "error": None,
                    "started_at": 100.0,
                    "finished_at": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(tmp_path)

    result = store.read_update_state()

    assert result.job.state == "restarting"
    assert result.job.restarting_at is None


def test_read_update_state_treats_an_unknown_job_state_as_idle(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    update_path = tmp_path / UPDATE_STATE_FILENAME
    update_path.write_text(
        json.dumps(
            {
                "latest": None,
                "checked_at": None,
                "previous_tag": None,
                "job": {
                    "state": "bogus",
                    "kind": "update",
                    "target": None,
                    "step": None,
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(tmp_path)

    with caplog.at_level(logging.WARNING):
        result = store.read_update_state()

    assert result == IDLE_UPDATE_STATE
    assert str(update_path) in caplog.text


def test_read_update_state_treats_an_unknown_job_kind_as_idle(tmp_path: Path) -> None:
    update_path = tmp_path / UPDATE_STATE_FILENAME
    update_path.write_text(
        json.dumps(
            {
                "latest": None,
                "checked_at": None,
                "previous_tag": None,
                "job": {
                    "state": "idle",
                    "kind": "bogus",
                    "target": None,
                    "step": None,
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(tmp_path)

    assert store.read_update_state() == IDLE_UPDATE_STATE


def test_read_update_state_treats_a_non_numeric_started_at_as_idle(tmp_path: Path) -> None:
    update_path = tmp_path / UPDATE_STATE_FILENAME
    update_path.write_text(
        json.dumps(
            {
                "latest": None,
                "checked_at": None,
                "previous_tag": None,
                "job": {
                    "state": "running",
                    "kind": "update",
                    "target": "v2.0.0",
                    "step": "fetch",
                    "error": None,
                    "started_at": "not-a-number",
                    "finished_at": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(tmp_path)

    assert store.read_update_state() == IDLE_UPDATE_STATE


def test_read_update_state_treats_a_non_string_target_as_idle(tmp_path: Path) -> None:
    update_path = tmp_path / UPDATE_STATE_FILENAME
    update_path.write_text(
        json.dumps(
            {
                "latest": None,
                "checked_at": None,
                "previous_tag": None,
                "job": {
                    "state": "running",
                    "kind": "update",
                    "target": 42,
                    "step": None,
                    "error": None,
                    "started_at": 100.0,
                    "finished_at": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(tmp_path)

    assert store.read_update_state() == IDLE_UPDATE_STATE


def test_read_update_state_treats_a_non_string_previous_tag_as_idle(tmp_path: Path) -> None:
    update_path = tmp_path / UPDATE_STATE_FILENAME
    update_path.write_text(
        json.dumps(
            {
                "latest": None,
                "checked_at": None,
                "previous_tag": 42,
                "job": {
                    "state": "idle",
                    "kind": "update",
                    "target": None,
                    "step": None,
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(tmp_path)

    assert store.read_update_state() == IDLE_UPDATE_STATE


def test_write_update_state_leaves_no_temp_file_behind(tmp_path: Path) -> None:
    store = JsonFileStateStore(tmp_path)

    store.write_update_state(IDLE_UPDATE_STATE)

    assert {p.name for p in tmp_path.iterdir()} == {UPDATE_STATE_FILENAME}


def test_read_update_state_treats_an_invalid_latest_tag_as_idle(tmp_path: Path) -> None:
    update_path = tmp_path / UPDATE_STATE_FILENAME
    update_path.write_text(
        json.dumps(
            {
                "latest": {
                    "tag": "-not-a-valid-tag",  # leading dash -- rejected by is_valid_release_tag
                    "name": "bad",
                    "published_at": "2026-01-01T00:00:00Z",
                    "html_url": "https://example.test",
                },
                "checked_at": None,
                "previous_tag": None,
                "job": {
                    "state": "idle",
                    "kind": "update",
                    "target": None,
                    "step": None,
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(tmp_path)

    assert store.read_update_state() == IDLE_UPDATE_STATE


def test_read_update_state_treats_a_non_string_latest_field_as_idle(tmp_path: Path) -> None:
    update_path = tmp_path / UPDATE_STATE_FILENAME
    update_path.write_text(
        json.dumps(
            {
                "latest": {
                    "tag": "v2.0.0",
                    "name": "v2.0.0",
                    "published_at": 12345,  # must be a string
                    "html_url": "https://example.test",
                },
                "checked_at": None,
                "previous_tag": None,
                "job": {
                    "state": "idle",
                    "kind": "update",
                    "target": None,
                    "step": None,
                    "error": None,
                    "started_at": None,
                    "finished_at": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(tmp_path)

    assert store.read_update_state() == IDLE_UPDATE_STATE


def test_read_update_state_salvages_latest_and_resets_job_to_idle_for_an_unrecognized_job_state(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    # Forward-compatibility: a downgrade to an older Portal build reading
    # an update.json a newer build already advanced into a job.state this
    # build has never heard of must not throw away a perfectly good
    # `latest`/`checked_at`/`previous_tag` record too.
    update_path = tmp_path / UPDATE_STATE_FILENAME
    latest_payload = {
        "tag": "v2.0.0",
        "name": "v2.0.0",
        "published_at": "2026-01-01T00:00:00Z",
        "html_url": "https://example.test",
    }
    update_path.write_text(
        json.dumps(
            {
                "latest": latest_payload,
                "checked_at": 123.0,
                "previous_tag": "v1.0.0",
                "job": {
                    "state": "quantum-uncertain",
                    "kind": "update",
                    "target": "v2.0.0",
                    "step": "sync",
                    "error": None,
                    "started_at": 100.0,
                    "finished_at": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(tmp_path)

    with caplog.at_level(logging.WARNING):
        result = store.read_update_state()

    assert result.latest == Release(
        tag="v2.0.0", name="v2.0.0", published_at="2026-01-01T00:00:00Z", html_url="https://example.test"
    )
    assert result.checked_at == 123.0
    assert result.previous_tag == "v1.0.0"
    assert result.job == IDLE_UPDATE_JOB
    assert "quantum-uncertain" in caplog.text


def test_read_update_state_salvages_latest_for_an_unrecognized_job_kind_too(tmp_path: Path) -> None:
    update_path = tmp_path / UPDATE_STATE_FILENAME
    latest_payload = {
        "tag": "v2.0.0",
        "name": "v2.0.0",
        "published_at": "2026-01-01T00:00:00Z",
        "html_url": "https://example.test",
    }
    update_path.write_text(
        json.dumps(
            {
                "latest": latest_payload,
                "checked_at": 123.0,
                "previous_tag": "v1.0.0",
                "job": {
                    "state": "running",
                    "kind": "sidegrade",  # unrecognized kind
                    "target": "v2.0.0",
                    "step": "sync",
                    "error": None,
                    "started_at": 100.0,
                    "finished_at": None,
                },
            }
        ),
        encoding="utf-8",
    )
    store = JsonFileStateStore(tmp_path)

    result = store.read_update_state()

    assert result.latest == Release(
        tag="v2.0.0", name="v2.0.0", published_at="2026-01-01T00:00:00Z", html_url="https://example.test"
    )
    assert result.job == IDLE_UPDATE_JOB
