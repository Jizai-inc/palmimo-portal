"""Shared download/verify/extract/swap machinery for the frontend build's GitHub Release asset.

One implementation, two callers:

- :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater` -- the
  ``assets``/``install-assets`` steps of a real device update.
- :mod:`palmimo_portal.fetch_static` -- a standalone CLI for a developer or
  tester who wants the built UI without installing Node.

Every function here is message-only: it raises :class:`StaticAssetError`
rather than :class:`~palmimo_portal.ports.UpdateStepError` (which needs a
*step name* neither this module nor the CLI has any business choosing) --
each caller wraps it into whatever shape fits its own error handling.
"""

from __future__ import annotations

import hashlib
import io
import logging
import os
import shutil
import tarfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Any


logger = logging.getLogger("palmimo_portal")


#: Per-request timeout for a download (the tarball, then its ``.sha256``
#: sidecar) -- generous for a Pi fetching a small (~470 KB uncompressed)
#: static bundle over a home network.
ASSET_TIMEOUT_SECONDS = 60.0

#: Hard cap on the tarball download -- the frontend bundle is a few hundred
#: KB, so refusing anything near 50 MB outright is cheaper and safer than
#: streaming an unbounded response into memory.
ASSET_MAX_BYTES = 50 * 1024 * 1024

#: Hard cap on any single extracted member's *uncompressed* size -- guards
#: against a gzip bomb exhausting disk during extraction even though the
#: compressed download passed :data:`ASSET_MAX_BYTES`.
MEMBER_MAX_BYTES = 20 * 1024 * 1024

#: Hard cap on the combined uncompressed size of every extracted member --
#: catches many members each under :data:`MEMBER_MAX_BYTES` that still sum
#: to something absurd.
TOTAL_MAX_BYTES = 200 * 1024 * 1024

#: What an ``opener`` callable is invoked with: a fully-built
#: :class:`urllib.request.Request` and the timeout in seconds. Must return a
#: context manager yielding an object with a ``.read()`` method.
Opener = Callable[[urllib.request.Request, float], Any]


class StaticAssetError(Exception):
    """Raised by every function in this module on a download/verify/extract/swap failure.

    Message-only -- callers translate it into their own error shape:
    :class:`~palmimo_portal.ports.UpdateStepError` in
    :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`, a CLI exit
    in :mod:`palmimo_portal.fetch_static`.
    """


def default_opener(request: urllib.request.Request, timeout: float) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)


def asset_url(update_repo: str, tag: str, asset_name: str) -> str:
    """Return the GitHub Release download URL for ``asset_name`` at ``tag``."""
    return f"https://github.com/{update_repo}/releases/download/{tag}/{asset_name}"


def download(
    opener: Opener,
    url: str,
    user_agent: str,
    *,
    timeout: float = ASSET_TIMEOUT_SECONDS,
    max_bytes: int = ASSET_MAX_BYTES,
    not_found_message: str | None = None,
) -> bytes:
    """Download ``url`` through ``opener``, capped at ``max_bytes``.

    Args:
        not_found_message: used verbatim as the error message on a 404,
            instead of the generic ``"HTTP 404 fetching <url>"`` -- lets a
            caller give a more actionable message without this module
            knowing about releases at all.

    Raises:
        StaticAssetError: the request failed, or the response exceeded
            ``max_bytes``.
    """
    request = urllib.request.Request(url, headers={"User-Agent": user_agent})
    try:
        with opener(request, timeout) as response:
            data = response.read(max_bytes + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404 and not_found_message is not None:
            raise StaticAssetError(not_found_message) from error
        raise StaticAssetError(f"HTTP {error.code} fetching {url}") from error
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise StaticAssetError(f"failed to fetch {url}: {error}") from error
    if len(data) > max_bytes:
        raise StaticAssetError(f"{url} exceeded the {max_bytes}-byte size cap")
    return data


def verify_checksum(asset_bytes: bytes, sha_bytes: bytes, asset_name: str) -> None:
    """Verify ``asset_bytes`` hashes to the digest named in ``sha_bytes`` (a ``sha256sum``-format line).

    Raises:
        StaticAssetError: the sidecar is empty/malformed, or the digest
            does not match.
    """
    expected_hex = sha_bytes.decode("utf-8", errors="replace").split()[:1]
    if not expected_hex:
        raise StaticAssetError(f"{asset_name}.sha256 is empty or malformed")
    actual_hex = hashlib.sha256(asset_bytes).hexdigest()
    if actual_hex.lower() != expected_hex[0].lower():
        raise StaticAssetError(f"checksum mismatch for {asset_name}: expected {expected_hex[0]}, got {actual_hex}")


def extract_to_staging(asset_bytes: bytes, temp_dir: Path, asset_name: str) -> None:
    """Safely unpack the ``static/...`` members of ``asset_bytes`` (a ``.tar.gz``) into ``temp_dir``.

    ``temp_dir`` is created fresh (any existing directory there is removed
    first) and removed again on any failure -- a partially-extracted staging
    directory must never be left for a caller to mistake for a complete one.

    Raises:
        StaticAssetError: a member is unsafe (symlink, traversal, outside
            the ``static/`` prefix, oversized), or the tarball itself
            cannot be read.
    """
    if temp_dir.exists():
        shutil.rmtree(temp_dir)
    temp_dir.mkdir(parents=True)
    try:
        with tarfile.open(fileobj=io.BytesIO(asset_bytes), mode="r:gz") as tar:
            total_bytes = 0
            for member in tar.getmembers():
                total_bytes = _extract_member(tar, member, temp_dir, asset_name, total_bytes)
    except Exception:
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise


def _extract_member(
    tar: tarfile.TarFile, member: tarfile.TarInfo, temp_dir: Path, asset_name: str, total_bytes: int
) -> int:
    """Extract one tar member, refusing anything that is not a plain file/directory under ``static/``.

    Defense against a compromised or malformed release asset: only
    ``member.isfile()``/``member.isdir()`` are accepted (symlinks, hardlinks,
    device/FIFO entries rejected outright); the member's path must resolve
    under the ``static/`` prefix with no ``..`` segment or absolute
    component; the on-disk target is re-checked to resolve inside
    ``temp_dir``; and a file member's claimed size is checked against
    :data:`MEMBER_MAX_BYTES`/:data:`TOTAL_MAX_BYTES` *before* it is read, so
    a gzip bomb never gets to write its inflated bytes to disk.

    Returns:
        The running total of uncompressed bytes extracted so far
        (``total_bytes`` plus this member's size, for a file member).
    """
    if not (member.isfile() or member.isdir()):
        raise StaticAssetError(f"unsafe member in {asset_name}: {member.name!r} is not a file or directory")

    member_path = PurePosixPath(member.name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise StaticAssetError(f"unsafe member path in {asset_name}: {member.name!r}")
    if not member_path.parts or member_path.parts[0] != "static":
        raise StaticAssetError(f"member path does not resolve under static/ in {asset_name}: {member.name!r}")

    relative_parts = member_path.parts[1:]
    if not relative_parts:
        return total_bytes  # the top-level "static" directory entry -- nothing to write

    target = temp_dir.joinpath(*relative_parts)
    try:
        target.resolve().relative_to(temp_dir.resolve())
    except ValueError as error:
        raise StaticAssetError(f"unsafe member path in {asset_name}: {member.name!r}") from error

    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        return total_bytes

    if member.size > MEMBER_MAX_BYTES:
        raise StaticAssetError(
            f"{asset_name} member {member.name!r} expands beyond the {MEMBER_MAX_BYTES // (1024 * 1024)} MB "
            "per-member uncompressed size cap"
        )
    total_bytes += member.size
    if total_bytes > TOTAL_MAX_BYTES:
        raise StaticAssetError(
            f"{asset_name} expands beyond the {TOTAL_MAX_BYTES // (1024 * 1024)} MB uncompressed total size cap"
        )

    target.parent.mkdir(parents=True, exist_ok=True)
    fileobj = tar.extractfile(member)
    if fileobj is None:
        raise StaticAssetError(f"could not read member {member.name!r} from {asset_name}")
    with open(target, "wb") as handle:
        shutil.copyfileobj(fileobj, handle)
    return total_bytes


def swap_into_place(temp_dir: Path, static_dir: Path) -> None:
    """Atomically-as-possible swap ``temp_dir`` in as ``static_dir``.

    The previous ``static_dir`` (if any) is renamed aside to
    ``static_dir.parent / "static.prev"` first, then ``temp_dir`` is renamed
    into ``static_dir``'s place; the backup is removed only once the swap
    fully succeeds. On a failed rename, the previous directory is restored
    best-effort so ``static_dir`` is never left missing.

    Raises:
        StaticAssetError: the rename failed.
    """
    prev_dir = static_dir.parent / "static.prev"
    if prev_dir.exists():
        shutil.rmtree(prev_dir)
    swapped_old_in = False
    try:
        if static_dir.exists():
            static_dir.rename(prev_dir)
            swapped_old_in = True
        temp_dir.rename(static_dir)
    except OSError as error:
        # Restore whatever was there before, on a best-effort basis -- a
        # failed rename here should never leave `static/` missing.
        if swapped_old_in and prev_dir.exists() and not static_dir.exists():
            prev_dir.rename(static_dir)
        shutil.rmtree(temp_dir, ignore_errors=True)
        raise StaticAssetError(f"failed to swap in the new static/: {error}") from error
    if swapped_old_in:
        shutil.rmtree(prev_dir, ignore_errors=True)


def _is_pid_alive(pid: int) -> bool:
    """Report whether *pid* names a process this machine still considers alive.

    ``os.kill(pid, 0)`` sends no signal, only checks whether the target
    could be signaled. :class:`ProcessLookupError` means genuinely gone --
    safe to treat as dead. :class:`PermissionError` means the process exists
    but is owned by someone else, so it must be treated as *alive*; guessing
    "dead" here could delete a staging directory a live process (e.g. a
    manual repair run as root) is still writing to.
    """
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def repair_static_dir(static_dir: Path) -> None:
    """Repair a ``static/`` left missing by a power loss mid-:func:`swap_into_place`, at boot.

    :func:`swap_into_place` renames the previous build aside to
    ``static.prev`` before renaming the new one into ``static_dir``'s place.
    A process killed between those two renames leaves ``static_dir`` absent,
    ``static.prev`` holding the last-known-good build, and possibly a
    ``static.tmp-<pid>`` sibling holding the build that was mid-swap.
    Without a repair, ``static_dir`` stays missing forever -- every page
    request 404s until an operator fixes it over SSH.

    Called unconditionally at startup, before ``_mount_frontend``; a no-op
    when ``static_dir`` already exists. Two independent repairs, every time:

    1. If ``static_dir`` is missing and ``static.prev`` exists, rename
       ``static.prev`` back -- restoring service, but on the **previous**
       build: by the time this runs, the Portal checkout is already on the
       *new* tag (``checkout`` runs before ``install-assets`` in
       :class:`~palmimo_portal.adapters.git_uv_updater.GitUvUpdater`'s step
       order), so the result is old frontend assets under a checkout that
       reports the new tag -- mismatched but safe. The update job is left
       showing failed at ``install-assets``, the honest signal that
       retrying re-downloads and re-swaps the new build, clearing the
       mismatch.
    2. Any ``static.tmp-*`` sibling (a staging directory whose owning
       process died before or during :func:`swap_into_place`) is removed,
       but only when the pid encoded in its name (``static.tmp-<pid>``) is
       confirmed dead via :func:`_is_pid_alive` -- a second, hand-started
       repair run must not delete a directory a live process is still
       writing to. A name that doesn't parse as ``static.tmp-<int>`` is
       treated the same as confirmed-dead and cleaned up.

    Every step is best-effort and logged, never raised: a filesystem
    problem here must not prevent the rest of the Portal (the API, which
    does not depend on ``static/``) from starting.
    """
    prev_dir = static_dir.parent / "static.prev"
    if not static_dir.exists() and prev_dir.is_dir():
        try:
            prev_dir.rename(static_dir)
            logger.warning(
                "static/ was missing at startup (likely a power loss mid-update) -- restored the previous "
                "frontend build from %s; the checkout is already on the new tag, so the update will show as "
                "failed at install-assets -- retry it to fetch and swap in the matching build",
                prev_dir,
            )
        except OSError as error:
            logger.error("failed to restore static/ from %s: %s", prev_dir, error)

    for orphan in sorted(static_dir.parent.glob("static.tmp-*")):
        pid_text = orphan.name.removeprefix("static.tmp-")
        try:
            pid = int(pid_text)
        except ValueError:
            pid = None
        if pid is not None and _is_pid_alive(pid):
            continue  # a live process still owns this directory
        try:
            shutil.rmtree(orphan)
            logger.info("removed orphaned update staging directory left over from an interrupted update: %s", orphan)
        except OSError as error:
            logger.error("failed to remove orphaned staging directory %s: %s", orphan, error)


def fetch_and_stage(
    opener: Opener,
    update_repo: str,
    tag: str,
    asset_name: str,
    user_agent: str,
    temp_dir: Path,
    *,
    not_found_message: str | None = None,
) -> None:
    """Download, checksum-verify, and extract one release's frontend asset into ``temp_dir``.

    The whole download half of the pipeline, as one call so both callers
    issue the same two requests in the same order.
    """
    base_url = asset_url(update_repo, tag, asset_name)
    asset_bytes = download(opener, base_url, user_agent, not_found_message=not_found_message)
    sha_bytes = download(opener, f"{base_url}.sha256", user_agent)
    verify_checksum(asset_bytes, sha_bytes, asset_name)
    extract_to_staging(asset_bytes, temp_dir, asset_name)
