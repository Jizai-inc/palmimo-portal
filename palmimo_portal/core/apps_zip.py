"""Safe zip extraction for an app install/update upload (design doc 3.4's "extract" row).

Rejects symlinks, hardlinks (indistinguishable from a regular file in a
zip's central directory, so caught by ``S_ISLNK`` on the entry's stored
unix mode instead), absolute paths, ``..`` traversal, backslash-separated
entries (a zip built on Windows can encode a Windows-style path Python's
``zipfile`` would not otherwise flag as unsafe on POSIX), duplicate entry
names, and device/FIFO entries. A single top-level directory (``myapp-1.0/``)
is peeled so the app root is always where ``palmimo.toml`` actually lives.
"""

from __future__ import annotations

import io
import shutil
import stat
import zipfile
from pathlib import Path, PurePosixPath

from palmimo_portal.ports import InvalidManifestSourceError


#: Cap on the multipart upload itself (design doc 3.4).
UPLOAD_MAX_BYTES = 200 * 1024 * 1024

#: Cap on the combined uncompressed size of every extracted member (design doc 3.4).
EXTRACTED_MAX_BYTES = 1024 * 1024 * 1024

_MANIFEST_NAME = "palmimo.toml"


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    mode = info.external_attr >> 16
    return stat.S_ISLNK(mode)


def _validate_member_path(name: str) -> PurePosixPath:
    if "\\" in name:
        raise InvalidManifestSourceError(f"zip entry uses a backslash path separator: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or ".." in path.parts:
        raise InvalidManifestSourceError(f"unsafe zip entry path: {name!r}")
    return path


def extract_zip_to_staging(source: bytes | Path, dest: Path) -> Path:
    """Safely extract ``source`` (a zip file, in memory or already staged on disk) into fresh directory ``dest``.

    ``source`` as a :class:`Path` reads the archive straight off disk
    (``zipfile.ZipFile`` seeks within it) instead of holding the whole
    upload in memory as ``bytes`` -- the streamed-upload half of design doc
    3.4's "fetch" step.

    Returns the app root: ``dest`` itself, or its sole child directory when
    the archive wraps everything in one top-level directory.

    Raises:
        InvalidManifestSourceError: any entry is unsafe, the archive is not
            a valid zip, the uncompressed total exceeds
            :data:`EXTRACTED_MAX_BYTES`, or ``palmimo.toml`` is not found at
            exactly one location at depth 0 or 1.
    """
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    try:
        _extract(source, dest)
    except BaseException:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    return _locate_app_root(dest)


def _extract(source: bytes | Path, dest: Path) -> None:
    try:
        archive = zipfile.ZipFile(io.BytesIO(source) if isinstance(source, bytes) else source)
    except zipfile.BadZipFile as error:
        raise InvalidManifestSourceError(f"not a valid zip file: {error}") from error

    with archive:
        infos = archive.infolist()
        seen: set[str] = set()
        total_bytes = 0
        for info in infos:
            path = _validate_member_path(info.filename)
            key = str(path)
            if key in seen:
                raise InvalidManifestSourceError(f"duplicate zip entry: {info.filename!r}")
            seen.add(key)

            if _is_symlink_entry(info):
                raise InvalidManifestSourceError(f"zip entry is a symlink: {info.filename!r}")
            mode = info.external_attr >> 16
            # Most zip writers (including Python's own `writestr`) store only
            # permission bits, with no S_IFREG/S_IFDIR file-type bits set at
            # all -- only explicitly dangerous types (device/fifo/socket) are
            # refused; an unset or ordinary-file mode is accepted.
            if mode and (stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)):
                raise InvalidManifestSourceError(f"zip entry is a device/fifo/socket: {info.filename!r}")

            if info.is_dir() or not path.parts:
                continue

            target = dest.joinpath(*path.parts)
            try:
                target.resolve().relative_to(dest.resolve())
            except ValueError as error:
                raise InvalidManifestSourceError(f"unsafe zip entry path: {info.filename!r}") from error

            total_bytes += info.file_size
            if total_bytes > EXTRACTED_MAX_BYTES:
                raise InvalidManifestSourceError(
                    f"zip expands beyond the {EXTRACTED_MAX_BYTES // (1024 * 1024)} MB uncompressed size cap"
                )

            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as entry_file, open(target, "wb") as handle:
                shutil.copyfileobj(entry_file, handle)
            _apply_executable_bits(target, mode)


#: Owner/group/other executable bits -- the only permission bits carried over from a zip
#: entry's stored unix mode. setuid/setgid (0o4000/0o2000) live above these and are dropped
#: by construction: an extracted app must never gain elevated privileges from its own zip.
_EXEC_BITS = stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH


def _apply_executable_bits(target: Path, mode: int) -> None:
    exec_bits = mode & _EXEC_BITS
    if exec_bits:
        target.chmod(target.stat().st_mode | exec_bits)


def _locate_app_root(dest: Path) -> Path:
    if (dest / _MANIFEST_NAME).is_file():
        return dest

    depth1_matches = [child for child in dest.iterdir() if child.is_dir() and (child / _MANIFEST_NAME).is_file()]
    if len(depth1_matches) == 1:
        return depth1_matches[0]
    if len(depth1_matches) > 1:
        raise InvalidManifestSourceError(
            f"multiple {_MANIFEST_NAME} files found at depth 1: {[str(p) for p in depth1_matches]}"
        )
    raise InvalidManifestSourceError(f"no {_MANIFEST_NAME} found at depth 0 or 1")
