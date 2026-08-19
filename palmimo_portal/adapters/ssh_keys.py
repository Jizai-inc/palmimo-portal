"""Real :class:`~palmimo_portal.ports.SshKeyPort`: reads and edits an ``authorized_keys`` file.

Path defaults to ``~/.ssh/authorized_keys`` (the fixed OS user), overridable
via ``PALMIMO_AUTHORIZED_KEYS`` for tests and local development.

Every mutation (:meth:`AuthorizedKeysSshKeyPort.add_key`,
:meth:`AuthorizedKeysSshKeyPort.delete_key`) holds an ``flock`` on a lockfile
next to the target across its whole read-modify-write, so concurrent callers
serialize instead of racing. This matters most for the last-key guard:
without it, two concurrent deletes could each see two keys left, each
conclude "not the last key", and together empty ``authorized_keys``, locking
the OS user out over SSH. Re-deriving "is this the last key" from inside the
lock, rather than trusting a value read before acquiring it, is what makes
the guard atomic.
"""

from __future__ import annotations

import contextlib
import fcntl
import logging
import os
import time
from collections.abc import Iterator
from pathlib import Path

from palmimo_portal.adapters.atomic_write import atomic_write_text, ensure_private_dir
from palmimo_portal.core.sshkey import fingerprint_key, parse_authorized_key
from palmimo_portal.ports import (
    DuplicateKeyError,
    InvalidKeyFormatError,
    KeyNotFoundError,
    LastKeyError,
    SshKey,
    SshKeyPort,
    SshKeysLockTimeoutError,
)


# Re-exported for backward compatibility. The implementation lives in
# palmimo_portal.core.sshkey (pure logic, no filesystem/D-Bus access) so
# palmimo_portal.testing.fakes can use it without importing from adapters/.
__all__ = [
    "AuthorizedKeysSshKeyPort",
    "default_authorized_keys_path",
    "fingerprint_key",
    "parse_authorized_key",
]


logger = logging.getLogger("palmimo_portal")

#: How long :meth:`AuthorizedKeysSshKeyPort._locked` waits to acquire its
#: lock before raising :class:`~palmimo_portal.ports.SshKeysLockTimeoutError`
#: -- mirrors :data:`~palmimo_portal.core.auth.AUTH_LOCK_TIMEOUT_SECONDS`.
SSH_KEYS_LOCK_TIMEOUT_SECONDS = 5.0

#: Retry interval for the non-blocking ``flock`` above -- mirrors
#: :data:`~palmimo_portal.adapters.state._AUTH_LOCK_POLL_INTERVAL_SECONDS`.
_SSH_KEYS_LOCK_POLL_INTERVAL_SECONDS = 0.05


def default_authorized_keys_path() -> Path:
    """Return ``PALMIMO_AUTHORIZED_KEYS`` if set, else ``~/.ssh/authorized_keys``."""
    override = os.environ.get("PALMIMO_AUTHORIZED_KEYS")
    if override:
        return Path(override)
    return Path.home() / ".ssh" / "authorized_keys"


class AuthorizedKeysSshKeyPort(SshKeyPort):
    """Reads and rewrites an ``authorized_keys`` file, preserving unrelated lines.

    Blank lines and lines this parser cannot make sense of (comments,
    options-prefixed entries) are kept verbatim on rewrite — this adapter
    only ever adds or removes whole lines it can itself parse and
    fingerprint.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._path = path if path is not None else default_authorized_keys_path()

    @contextlib.contextmanager
    def _locked(self) -> Iterator[None]:
        """Hold an exclusive ``flock`` on a lockfile next to ``self._path`` for the block's duration, bounded.

        A dedicated ``.lock`` file, not ``self._path`` itself, is locked --
        locking the target directly would interact awkwardly with
        :func:`~palmimo_portal.adapters.atomic_write.atomic_write_text`'s
        rename-a-new-inode-into-place write path. Retries a non-blocking
        ``flock`` every :data:`_SSH_KEYS_LOCK_POLL_INTERVAL_SECONDS` up to
        :data:`SSH_KEYS_LOCK_TIMEOUT_SECONDS`, logging a WARNING the moment
        it starts waiting.

        Raises:
            SshKeysLockTimeoutError: the lock could not be acquired within
                :data:`SSH_KEYS_LOCK_TIMEOUT_SECONDS`.
        """
        lock_path = self._path.with_name(f".{self._path.name}.lock")
        ensure_private_dir(self._path.parent)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            deadline = time.monotonic() + SSH_KEYS_LOCK_TIMEOUT_SECONDS
            waited = False
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if not waited:
                        logger.warning(
                            "authorized_keys lock contended, waiting up to %gs: %s",
                            SSH_KEYS_LOCK_TIMEOUT_SECONDS,
                            lock_path,
                        )
                        waited = True
                    if time.monotonic() >= deadline:
                        raise SshKeysLockTimeoutError() from None
                    time.sleep(_SSH_KEYS_LOCK_POLL_INTERVAL_SECONDS)
            try:
                yield
            finally:
                fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)

    def _read_lines(self) -> list[str]:
        if not self._path.is_file():
            return []
        return self._path.read_text(encoding="utf-8").splitlines()

    def _write_lines(self, lines: list[str]) -> None:
        text = "\n".join(lines)
        if text and not text.endswith("\n"):
            text += "\n"
        try:
            atomic_write_text(self._path, text)
        except OSError as error:
            logger.error("authorized_keys %s: failed to write (%s)", self._path, error)
            raise

    @staticmethod
    def _parsed_entries_from(lines: list[str]) -> list[tuple[str, str, str]]:
        """Return ``(line, key_type, comment)`` for every line this adapter can parse, out of an already-read ``lines``.

        Takes ``lines`` rather than re-reading the file so a caller holding
        :meth:`_locked` derives both "how many keys" and "which line matches
        this fingerprint" from one consistent snapshot.
        """
        entries = []
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            try:
                key_type, comment = parse_authorized_key(stripped)
            except InvalidKeyFormatError:
                continue
            entries.append((stripped, key_type, comment))
        return entries

    def _parsed_entries(self) -> list[tuple[str, str, str]]:
        return self._parsed_entries_from(self._read_lines())

    def list_keys(self) -> list[SshKey]:
        return [
            SshKey(fingerprint=fingerprint_key(line), key_type=key_type, comment=comment)
            for line, key_type, comment in self._parsed_entries()
        ]

    def add_key(self, public_key: str) -> SshKey:
        key_type, comment = parse_authorized_key(public_key)
        normalized = public_key.strip()
        fingerprint = fingerprint_key(normalized)
        with self._locked():
            lines = self._read_lines()
            entries = self._parsed_entries_from(lines)
            if any(fingerprint_key(line) == fingerprint for line, _, _ in entries):
                raise DuplicateKeyError(fingerprint)
            lines.append(normalized)
            self._write_lines(lines)
        logger.info("authorized_keys %s: added %s", self._path, fingerprint)
        return SshKey(fingerprint=fingerprint, key_type=key_type, comment=comment)

    def delete_key(self, fingerprint: str, *, allow_last: bool = False) -> None:
        with self._locked():
            lines = self._read_lines()
            entries = self._parsed_entries_from(lines)
            if not any(fingerprint_key(line) == fingerprint for line, _, _ in entries):
                raise KeyNotFoundError(fingerprint)
            if len(entries) == 1 and not allow_last:
                raise LastKeyError(fingerprint)
            kept = []
            for line in lines:
                stripped = line.strip()
                if stripped and not stripped.startswith("#"):
                    try:
                        parse_authorized_key(stripped)
                    except InvalidKeyFormatError:
                        kept.append(line)
                        continue
                    if fingerprint_key(stripped) == fingerprint:
                        continue
                kept.append(line)
            self._write_lines(kept)
        logger.info("authorized_keys %s: removed %s", self._path, fingerprint)
