"""Shared helper: atomic, permission-safe writes for the Portal's real adapters.

Every real adapter that persists secret material (``auth.json``'s password
hash/signing key, ``authorized_keys``) writes through
:func:`atomic_write_text`, not ``Path.write_text`` directly:

- **Durability.** Writes to a temp file in the same directory and
  ``os.replace()``-s it onto the target (POSIX-atomic). ``os.replace``
  alone is not enough: without an explicit ``fsync()`` of the temp file
  before the rename, and of the *directory* after it, a power loss can
  still lose the write or leave the directory entry pointing at nothing.
- **No permissive window.** ``tempfile.mkstemp`` opens with mode ``0600``
  as part of the same ``os.open`` call that creates it, rather than
  ``chmod``-ing afterward, so no interval exists where a wider mode is observable.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path


_PRIVATE_DIR_MODE = 0o700


@contextlib.contextmanager
def _restrictive_umask() -> Iterator[None]:
    """Temporarily set the process umask so newly created directories stay private.

    ``os.umask`` only narrows the mode passed to ``mkdir``, never widens
    it, so ``umask(0o077)`` guarantees no group/other bits regardless of
    the ambient umask.
    """
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def ensure_private_dir(path: Path) -> None:
    """Create ``path`` (and any missing parents) with mode ``0700``. No-op if it already exists; existing mode is left as-is."""
    with _restrictive_umask():
        path.mkdir(parents=True, exist_ok=True, mode=_PRIVATE_DIR_MODE)


def fsync_dir(path: Path) -> None:
    """fsync a directory's entries, so a completed rename into it survives a power loss.

    ``os.fsync`` on a regular file guarantees only the file's own data and
    metadata are on disk, not the directory entry pointing at it -- after
    ``os.replace()``, the directory itself must be fsynced too.
    """
    dir_fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def atomic_write_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` as a private (mode ``0600``) file.

    Creates ``path``'s parent (mode ``0700``) if missing, writes to a temp
    file in the same directory (rename stays on one filesystem), fsyncs it,
    renames onto ``path`` with :func:`os.replace`, and fsyncs the parent --
    a power loss right after this call returns cannot lose or empty the file.

    Raises:
        OSError: directory creation, the temp-file write, or the rename
            failed. The temp file is cleaned up before the error propagates.
    """
    ensure_private_dir(path.parent)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
        fsync_dir(path.parent)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def create_exclusive_text(path: Path, text: str) -> None:
    """Create ``path`` with ``text``, atomically, if and only if it does not already exist.

    Used for state that must be created exactly once under concurrent
    writers (see ``StateStore.create_auth``): ``os.replace`` always
    overwrites silently, so only a direct ``O_CREAT | O_EXCL`` open of the
    final path makes "exactly one concurrent creator wins" atomic. Fsyncs
    the file and parent directory before returning, same as :func:`atomic_write_text`.

    Raises:
        FileExistsError: another writer already created ``path`` -- the
            caller lost the creation race and must not overwrite it.
        OSError: directory creation or the write failed. A file this call
            created itself is removed before the error propagates.
    """
    ensure_private_dir(path.parent)
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        fsync_dir(path.parent)
    except BaseException:
        path.unlink(missing_ok=True)
        raise
