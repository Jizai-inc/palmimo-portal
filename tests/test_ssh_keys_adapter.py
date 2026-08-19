"""Tests for the real ``authorized_keys``-backed :class:`SshKeyPort` adapter."""

from __future__ import annotations

import base64
import stat
import struct
import threading
from pathlib import Path

import pytest

from palmimo_portal.adapters.ssh_keys import AuthorizedKeysSshKeyPort, fingerprint_key, parse_authorized_key
from palmimo_portal.ports import (
    DuplicateKeyError,
    InvalidKeyFormatError,
    KeyNotFoundError,
    LastKeyError,
    SshKeysLockTimeoutError,
)


def _wire_blob(key_type: str, payload: bytes) -> str:
    """Build a structurally valid SSH wire-format blob: a 4-byte length-prefixed type string, then payload."""
    type_bytes = key_type.encode("ascii")
    wire = struct.pack(">I", len(type_bytes)) + type_bytes + payload
    return base64.b64encode(wire).decode("ascii")


def _key(material: bytes, comment: str = "user@laptop", key_type: str = "ssh-ed25519") -> str:
    blob = _wire_blob(key_type, material)
    return f"{key_type} {blob} {comment}"


KEY_A = _key(b"key-material-a-000000000000", comment="alice@laptop")
KEY_B = _key(b"key-material-b-000000000000", comment="bob@desktop")


def test_parse_authorized_key_returns_type_and_comment() -> None:
    key_type, comment = parse_authorized_key(KEY_A)

    assert key_type == "ssh-ed25519"
    assert comment == "alice@laptop"


def test_parse_authorized_key_rejects_an_unknown_type() -> None:
    with pytest.raises(InvalidKeyFormatError):
        parse_authorized_key("not-a-key-type AAAA comment")


def test_parse_authorized_key_rejects_invalid_base64() -> None:
    with pytest.raises(InvalidKeyFormatError):
        parse_authorized_key("ssh-ed25519 not-valid-base64!!! comment")


def test_parse_authorized_key_rejects_a_single_token() -> None:
    with pytest.raises(InvalidKeyFormatError):
        parse_authorized_key("ssh-ed25519")


def test_parse_authorized_key_rejects_a_blob_whose_wire_type_does_not_match_the_declared_type() -> None:
    # Structurally valid base64, and structurally valid SSH wire format --
    # but the type embedded in the wire-format blob (ssh-rsa) does not
    # match the type field on the line (ssh-ed25519). Before the fix, any
    # non-empty base64 would pass regardless of what it actually decoded to.
    mismatched = _key(b"payload", key_type="ssh-rsa").replace("ssh-rsa ", "ssh-ed25519 ", 1)

    with pytest.raises(InvalidKeyFormatError):
        parse_authorized_key(mismatched)


def test_parse_authorized_key_rejects_base64_that_is_not_wire_format_at_all() -> None:
    # Valid base64 of arbitrary bytes that happen to not even parse as a
    # length-prefixed wire-format blob (the "any non-empty base64" bug this
    # fix closes).
    garbage_blob = base64.b64encode(b"not-a-real-ssh-key-blob").decode("ascii")

    with pytest.raises(InvalidKeyFormatError):
        parse_authorized_key(f"ssh-ed25519 {garbage_blob} comment")


def test_parse_authorized_key_rejects_a_wire_format_length_prefix_that_overruns_the_blob() -> None:
    # A length prefix larger than the remaining bytes -- a truncated or
    # corrupted key, not merely "some non-empty base64".
    type_bytes = b"ssh-ed25519"
    truncated = struct.pack(">I", len(type_bytes) + 100) + type_bytes
    blob = base64.b64encode(truncated).decode("ascii")

    with pytest.raises(InvalidKeyFormatError):
        parse_authorized_key(f"ssh-ed25519 {blob} comment")


def test_fingerprint_is_stable_and_type_prefixed() -> None:
    fingerprint = fingerprint_key(KEY_A)

    assert fingerprint.startswith("SHA256:")
    assert fingerprint == fingerprint_key(KEY_A)
    assert fingerprint != fingerprint_key(KEY_B)


def test_list_keys_is_empty_for_a_missing_file(tmp_path: Path) -> None:
    port = AuthorizedKeysSshKeyPort(tmp_path / "authorized_keys")

    assert port.list_keys() == []


def test_add_key_then_list_returns_it(tmp_path: Path) -> None:
    port = AuthorizedKeysSshKeyPort(tmp_path / "authorized_keys")

    added = port.add_key(KEY_A)

    [listed] = port.list_keys()
    assert listed == added
    assert listed.comment == "alice@laptop"


def test_add_key_rejects_invalid_format(tmp_path: Path) -> None:
    port = AuthorizedKeysSshKeyPort(tmp_path / "authorized_keys")

    with pytest.raises(InvalidKeyFormatError):
        port.add_key("garbage")


def test_add_key_is_not_idempotent_on_a_duplicate(tmp_path: Path) -> None:
    port = AuthorizedKeysSshKeyPort(tmp_path / "authorized_keys")
    port.add_key(KEY_A)

    with pytest.raises(DuplicateKeyError):
        port.add_key(KEY_A)


def test_add_key_writes_the_file_with_mode_0600(tmp_path: Path) -> None:
    path = tmp_path / "authorized_keys"
    port = AuthorizedKeysSshKeyPort(path)

    port.add_key(KEY_A)

    mode = stat.S_IMODE(path.stat().st_mode)
    assert mode == 0o600


def test_add_key_preserves_unrelated_lines(tmp_path: Path) -> None:
    path = tmp_path / "authorized_keys"
    path.write_text('# a comment\n\ncommand="restricted" ssh-rsa AAAA opaque-options-entry\n', encoding="utf-8")
    port = AuthorizedKeysSshKeyPort(path)

    port.add_key(KEY_A)

    text = path.read_text(encoding="utf-8")
    assert "# a comment" in text
    assert 'command="restricted" ssh-rsa AAAA opaque-options-entry' in text
    assert KEY_A in text


def test_delete_key_removes_only_the_matching_line(tmp_path: Path) -> None:
    path = tmp_path / "authorized_keys"
    port = AuthorizedKeysSshKeyPort(path)
    added_a = port.add_key(KEY_A)
    port.add_key(KEY_B)

    port.delete_key(added_a.fingerprint)

    [remaining] = port.list_keys()
    assert remaining.comment == "bob@desktop"


def test_delete_key_raises_when_fingerprint_is_unknown(tmp_path: Path) -> None:
    port = AuthorizedKeysSshKeyPort(tmp_path / "authorized_keys")

    with pytest.raises(KeyNotFoundError):
        port.delete_key("SHA256:doesnotexist")


def test_delete_key_raises_last_key_error_without_allow_last(tmp_path: Path) -> None:
    port = AuthorizedKeysSshKeyPort(tmp_path / "authorized_keys")
    added = port.add_key(KEY_A)

    with pytest.raises(LastKeyError):
        port.delete_key(added.fingerprint)

    [remaining] = port.list_keys()
    assert remaining == added


def test_delete_key_with_allow_last_succeeds(tmp_path: Path) -> None:
    port = AuthorizedKeysSshKeyPort(tmp_path / "authorized_keys")
    added = port.add_key(KEY_A)

    port.delete_key(added.fingerprint, allow_last=True)

    assert port.list_keys() == []


def test_concurrent_deletes_of_the_last_key_do_not_both_succeed(tmp_path: Path) -> None:
    # Two AuthorizedKeysSshKeyPort instances pointed at the same file model
    # two concurrent DELETE requests. Before the fix, list_keys()+delete_key()
    # was check-then-act across two separate calls: both threads could each
    # observe "1 key left" is false (2 keys), or in the single-key case each
    # observe "this is not the last key" incorrectly, and both proceed --
    # together deleting the only key without ever passing allow_last=True.
    # With the guard inside delete_key under flock, at most one thread can
    # ever succeed without allow_last, and the other must see LastKeyError.
    path = tmp_path / "authorized_keys"
    setup_port = AuthorizedKeysSshKeyPort(path)
    only_key = setup_port.add_key(KEY_A)

    port_a = AuthorizedKeysSshKeyPort(path)
    port_b = AuthorizedKeysSshKeyPort(path)
    outcomes: list[str] = []
    lock = threading.Lock()

    def attempt(port: AuthorizedKeysSshKeyPort) -> None:
        try:
            port.delete_key(only_key.fingerprint)
            outcome = "deleted"
        except LastKeyError:
            outcome = "blocked"
        with lock:
            outcomes.append(outcome)

    threads = [threading.Thread(target=attempt, args=(port_a,)), threading.Thread(target=attempt, args=(port_b,))]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("blocked") == 2
    assert [key.fingerprint for key in setup_port.list_keys()] == [only_key.fingerprint]


def test_default_path_honors_the_env_override(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from palmimo_portal.adapters.ssh_keys import default_authorized_keys_path

    override = tmp_path / "custom_authorized_keys"
    monkeypatch.setenv("PALMIMO_AUTHORIZED_KEYS", str(override))

    assert default_authorized_keys_path() == override


def test_parse_authorized_key_rejects_an_embedded_newline() -> None:
    with pytest.raises(InvalidKeyFormatError):
        parse_authorized_key(KEY_A + "\nssh-rsa AAAA injected")


def test_parse_authorized_key_rejects_an_embedded_carriage_return() -> None:
    with pytest.raises(InvalidKeyFormatError):
        parse_authorized_key(KEY_A + "\rssh-rsa AAAA injected")


def test_add_key_rejects_a_multiline_public_key(tmp_path: Path) -> None:
    path = tmp_path / "authorized_keys"
    port = AuthorizedKeysSshKeyPort(path)
    malicious = KEY_A + "\nssh-rsa AAAA injected-line evil@attacker"

    with pytest.raises(InvalidKeyFormatError):
        port.add_key(malicious)

    assert not path.exists()


def test_add_key_rejects_a_multiline_public_key_leaving_the_existing_file_unchanged(tmp_path: Path) -> None:
    path = tmp_path / "authorized_keys"
    port = AuthorizedKeysSshKeyPort(path)
    port.add_key(KEY_A)
    before = path.read_text(encoding="utf-8")
    malicious = KEY_B + "\nssh-rsa AAAA injected-line evil@attacker"

    with pytest.raises(InvalidKeyFormatError):
        port.add_key(malicious)

    assert path.read_text(encoding="utf-8") == before
    assert "injected-line" not in before


def test_add_key_rejects_a_multiline_public_key_that_no_line_parses_alone(tmp_path: Path) -> None:
    # Guards against a weaker fix that only rejects when parse_authorized_key's
    # split(None, 2) happens to swallow the injected line into the comment: a
    # payload where the injected second line is itself a well-formed key must
    # still be rejected outright, not silently added as just the first line.
    path = tmp_path / "authorized_keys"
    port = AuthorizedKeysSshKeyPort(path)
    malicious = f"{KEY_A}\n{KEY_B}"

    with pytest.raises(InvalidKeyFormatError):
        port.add_key(malicious)

    assert not path.exists()


# -- _locked() -- bounded flock, mirrors JsonFileStateStore.lock_auth ------


def test_locked_blocks_a_concurrent_holder(tmp_path: Path) -> None:
    path = tmp_path / "authorized_keys"
    port_a = AuthorizedKeysSshKeyPort(path)
    port_b = AuthorizedKeysSshKeyPort(path)

    entered_second = threading.Event()

    def second_holder() -> None:
        with port_b._locked():
            entered_second.set()

    with port_a._locked():
        thread = threading.Thread(target=second_holder)
        thread.start()
        assert not entered_second.wait(timeout=0.2)

    assert entered_second.wait(timeout=5.0)
    thread.join(timeout=5.0)


def test_locked_raises_ssh_keys_lock_timeout_after_the_bounded_wait(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A short timeout keeps this test fast without weakening what it
    # proves: a contender that cannot acquire the lock within the budget
    # must raise rather than block forever -- see delete_key's/add_key's
    # last-key guard docstring for why an unbounded wait here would be
    # unacceptable (one stuck caller hanging every other key-management
    # request indefinitely).
    monkeypatch.setattr("palmimo_portal.adapters.ssh_keys.SSH_KEYS_LOCK_TIMEOUT_SECONDS", 0.2)
    path = tmp_path / "authorized_keys"
    port_a = AuthorizedKeysSshKeyPort(path)
    port_b = AuthorizedKeysSshKeyPort(path)
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with port_a._locked():
            holding.set()
            release.wait(timeout=5.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert holding.wait(timeout=2.0)

    with pytest.raises(SshKeysLockTimeoutError), port_b._locked():
        pass  # pragma: no cover -- must never be entered

    release.set()
    thread.join(timeout=5.0)


def test_locked_logs_a_warning_once_contention_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    monkeypatch.setattr("palmimo_portal.adapters.ssh_keys.SSH_KEYS_LOCK_TIMEOUT_SECONDS", 0.2)
    path = tmp_path / "authorized_keys"
    port_a = AuthorizedKeysSshKeyPort(path)
    port_b = AuthorizedKeysSshKeyPort(path)
    holding = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with port_a._locked():
            holding.set()
            release.wait(timeout=5.0)

    thread = threading.Thread(target=holder)
    thread.start()
    assert holding.wait(timeout=2.0)

    with caplog.at_level(logging.WARNING), pytest.raises(SshKeysLockTimeoutError), port_b._locked():
        pass  # pragma: no cover -- must never be entered

    release.set()
    thread.join(timeout=5.0)

    assert "authorized_keys lock contended" in caplog.text
    assert not thread.is_alive()


def test_add_key_succeeds_normally_when_the_lock_is_uncontended(tmp_path: Path) -> None:
    path = tmp_path / "authorized_keys"
    port = AuthorizedKeysSshKeyPort(path)

    key = port.add_key(KEY_A)

    assert key.fingerprint == fingerprint_key(KEY_A)
    assert [entry.fingerprint for entry in port.list_keys()] == [key.fingerprint]
