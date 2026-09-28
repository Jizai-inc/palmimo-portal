"""Real :class:`~palmimo_portal.ports.PlatformPort` and :class:`~palmimo_portal.ports.PlatformBundleSource`.

The only file (besides ``settings.py``, where the field is declared) that
may reference :attr:`~palmimo_portal.settings.Settings.sudo_bin` or the
literal ``"sudo"`` -- ``tests/test_platform_sudo_contract.py`` greps the
tree for both. Every ``install.sh`` verb (``verify``/``install``/``record``)
shells out through ``sudo``, argv-list only (never a shell string), with a
bounded timeout per verb.

:class:`GitHubPlatformBundleSource` needs no elevated privilege at all --
it only downloads into a Portal-owned staging directory -- so it shares
:mod:`palmimo_portal.adapters.static_asset`'s generic ``download``/
``verify_checksum`` primitives but implements its own extraction: the
platform bundle has no fixed ``static/`` path prefix the way the frontend
build asset does.
"""

from __future__ import annotations

import io
import json
import logging
import shutil
import subprocess
import tarfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

from palmimo_portal.adapters.static_asset import (
    Opener,
    StaticAssetError,
    asset_url,
    default_opener,
    download,
    verify_checksum,
)
from palmimo_portal.ports import (
    PlatformBundleFetchError,
    PlatformCommandError,
    PlatformInstalled,
    PlatformManifest,
    PlatformPort,
    PlatformVerifyDiff,
)
from palmimo_portal.version import portal_version


logger = logging.getLogger("palmimo_portal")

#: The `timeout <secs>` argv wrapper's own budget -- what `install.sh` itself is allowed to run
#: for. The subprocess timeout below it is always larger, so a `timeout`/`sudo` process that
#: outlives its SIGTERM+SIGKILL sequence (the `-k 5` grace period) is still caught by Python
#: rather than left to hang the request forever.
VERIFY_TIMEOUT_SECONDS = 60.0
INSTALL_TIMEOUT_SECONDS = 1800.0
RECORD_TIMEOUT_SECONDS = 30.0

VERIFY_SUBPROCESS_TIMEOUT_SECONDS = 90.0
INSTALL_SUBPROCESS_TIMEOUT_SECONDS = 1860.0
RECORD_SUBPROCESS_TIMEOUT_SECONDS = 60.0

#: `install.sh verify`'s own exit-code contract: 0 = clean, 1 = differences found (still a
#: successful run -- the diffs are its stdout), >=2 = the command itself failed.
_VERIFY_SUCCESS_CODES = (0, 1)

#: Mirrors adapters/static_asset.py's gzip-bomb guard -- the platform bundle
#: (install.sh, manifest.json, a handful of unit/polkit/tmpfiles files) is small.
MEMBER_MAX_BYTES = 20 * 1024 * 1024
TOTAL_MAX_BYTES = 200 * 1024 * 1024


@dataclass
class SudoPlatformPort(PlatformPort):
    """Reads ``installed.json`` directly (no privilege needed) and runs ``install.sh`` via ``sudo``."""

    installed_path: Path
    sudo_bin: str = "sudo"
    runner: object = field(default=subprocess.run)

    def read_installed(self) -> PlatformInstalled | None:
        if not self.installed_path.is_file():
            return None
        try:
            data: Any = json.loads(self.installed_path.read_text(encoding="utf-8"))
            return PlatformInstalled(
                version=int(data["version"]),
                installed_at=str(data["installed_at"]),
                bundle_sha256=str(data["bundle_sha256"]),
            )
        except (OSError, ValueError, KeyError, TypeError) as error:
            logger.error("platform: installed.json is corrupt or unreadable: %s (%s)", self.installed_path, error)
            return None

    def verify(self, bundle_dir: Path) -> list[PlatformVerifyDiff]:
        output = self._run(
            "verify",
            [str(bundle_dir / "install.sh"), "verify", "--root", "/"],
            command_timeout=VERIFY_TIMEOUT_SECONDS,
            subprocess_timeout=VERIFY_SUBPROCESS_TIMEOUT_SECONDS,
            success_codes=_VERIFY_SUCCESS_CODES,
        )
        diffs: list[PlatformVerifyDiff] = []
        for raw_line in output.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload: Any = json.loads(line)
                diffs.append(
                    PlatformVerifyDiff(
                        path=str(payload["path"]),
                        kind=str(payload["kind"]),
                        expected=str(payload["expected"]) if "expected" in payload else None,
                        actual=str(payload["actual"]) if "actual" in payload else None,
                    )
                )
            except (json.JSONDecodeError, KeyError, TypeError) as error:
                raise PlatformCommandError("verify", f"unparseable verify output line {line!r}: {error}") from error
        return diffs

    def install(self, bundle_dir: Path) -> None:
        self._run(
            "install",
            [str(bundle_dir / "install.sh"), "install", "--root", "/"],
            command_timeout=INSTALL_TIMEOUT_SECONDS,
            subprocess_timeout=INSTALL_SUBPROCESS_TIMEOUT_SECONDS,
        )

    def record(self, bundle_dir: Path, sha: str) -> None:
        self._run(
            "record",
            [str(bundle_dir / "install.sh"), "record", "--root", "/", "--sha", sha],
            command_timeout=RECORD_TIMEOUT_SECONDS,
            subprocess_timeout=RECORD_SUBPROCESS_TIMEOUT_SECONDS,
        )

    def _run(
        self,
        step: str,
        argv: list[str],
        *,
        command_timeout: float,
        subprocess_timeout: float,
        success_codes: tuple[int, ...] = (0,),
    ) -> str:
        # `-n`: never fall back to a password prompt no one can answer. `timeout -k 5 <secs>`:
        # a bounded budget enforced on the command itself, independent of (and smaller than) the
        # subprocess-level timeout below, which exists only to catch `timeout`/`sudo` itself hanging.
        full_argv = [self.sudo_bin, "-n", "timeout", "-k", "5", str(int(command_timeout)), *argv]
        try:
            result = self.runner(  # type: ignore[operator]
                full_argv, capture_output=True, text=True, timeout=subprocess_timeout
            )
        except subprocess.TimeoutExpired as error:
            raise PlatformCommandError(step, f"timed out after {subprocess_timeout:g}s") from error
        except OSError as error:
            raise PlatformCommandError(step, f"failed to start: {error}") from error
        if result.returncode not in success_codes:
            tail = (result.stderr or "").strip()
            raise PlatformCommandError(step, tail or f"exited {result.returncode}")
        return result.stdout


@dataclass
class GitHubPlatformBundleSource:
    """Downloads/verifies/extracts one ``palmimo-image`` platform bundle release."""

    platform_repo: str
    opener: Opener = field(default=default_opener)

    def fetch_latest_manifest(self, tag: str) -> PlatformManifest:
        asset_name = f"palmimo-platform-{tag}.manifest.json"
        base_url = asset_url(self.platform_repo, tag, asset_name)
        user_agent = f"palmimo-portal/{portal_version()}"
        try:
            asset_bytes = download(self.opener, base_url, user_agent)
            sha_bytes = download(self.opener, f"{base_url}.sha256", user_agent)
            verify_checksum(asset_bytes, sha_bytes, asset_name)
        except StaticAssetError as error:
            raise PlatformBundleFetchError("fetch_manifest", str(error)) from error
        try:
            data: Any = json.loads(asset_bytes.decode("utf-8"))
            return PlatformManifest(
                version=int(data["version"]),
                requires_portal=str(data["requires_portal"]),
                restart_portal=bool(data["restart_portal"]),
                summary=str(data["summary"]),
                reflash_required=bool(data.get("reflash_required", False)),
            )
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
            raise PlatformBundleFetchError("fetch_manifest", f"malformed {asset_name}: {error}") from error

    def fetch(self, tag: str, dest_dir: Path, on_step: Callable[[str], None]) -> tuple[Path, str]:
        asset_name = f"palmimo-platform-{tag}.tar.gz"
        base_url = asset_url(self.platform_repo, tag, asset_name)
        user_agent = f"palmimo-portal/{portal_version()}"

        on_step("fetch")
        try:
            asset_bytes = download(self.opener, base_url, user_agent)
        except StaticAssetError as error:
            raise PlatformBundleFetchError("fetch", str(error)) from error

        on_step("verify_sha")
        try:
            sha_bytes = download(self.opener, f"{base_url}.sha256", user_agent)
            verify_checksum(asset_bytes, sha_bytes, asset_name)
        except StaticAssetError as error:
            raise PlatformBundleFetchError("verify_sha", str(error)) from error
        sha_hex = sha_bytes.decode("utf-8", errors="replace").split()[0].lower()

        on_step("extract")
        target = dest_dir / "staging"
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        try:
            bundle_dir = _extract_bundle(asset_bytes, target, asset_name)
        except StaticAssetError as error:
            shutil.rmtree(target, ignore_errors=True)
            raise PlatformBundleFetchError("extract", str(error)) from error
        return bundle_dir, sha_hex


def _extract_bundle(asset_bytes: bytes, dest_dir: Path, asset_name: str) -> Path:
    """Safely unpack *asset_bytes* (a ``.tar.gz``) into ``dest_dir``, returning the directory holding ``install.sh``.

    Accepts a tarball with every member at its root, or one wrapped in a
    single common top-level directory -- the exact shape
    ``tools/build_platform_bundle.py`` (palmimo-image) produces is not
    observable from this repository, so both are handled.
    """
    with tarfile.open(fileobj=io.BytesIO(asset_bytes), mode="r:gz") as tar:
        total_bytes = 0
        for member in tar.getmembers():
            total_bytes = _extract_member(tar, member, dest_dir, asset_name, total_bytes)
    return _find_bundle_root(dest_dir, asset_name)


def _find_bundle_root(dest_dir: Path, asset_name: str) -> Path:
    if (dest_dir / "install.sh").is_file():
        return dest_dir
    for child in sorted(dest_dir.iterdir()):
        if child.is_dir() and (child / "install.sh").is_file():
            return child
    raise StaticAssetError(f"{asset_name} did not contain an install.sh")


def _extract_member(
    tar: tarfile.TarFile, member: tarfile.TarInfo, dest_dir: Path, asset_name: str, total_bytes: int
) -> int:
    if not (member.isfile() or member.isdir()):
        raise StaticAssetError(f"unsafe member in {asset_name}: {member.name!r} is not a file or directory")
    member_path = PurePosixPath(member.name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise StaticAssetError(f"unsafe member path in {asset_name}: {member.name!r}")
    if not member_path.parts:
        return total_bytes

    target = dest_dir.joinpath(*member_path.parts)
    try:
        target.resolve().relative_to(dest_dir.resolve())
    except ValueError as error:
        raise StaticAssetError(f"unsafe member path in {asset_name}: {member.name!r}") from error

    if member.isdir():
        target.mkdir(parents=True, exist_ok=True)
        return total_bytes

    if member.size > MEMBER_MAX_BYTES:
        raise StaticAssetError(f"{asset_name} member {member.name!r} exceeds the per-member size cap")
    total_bytes += member.size
    if total_bytes > TOTAL_MAX_BYTES:
        raise StaticAssetError(f"{asset_name} exceeds the total uncompressed size cap")

    target.parent.mkdir(parents=True, exist_ok=True)
    fileobj = tar.extractfile(member)
    if fileobj is None:
        raise StaticAssetError(f"could not read member {member.name!r} from {asset_name}")
    with open(target, "wb") as handle:
        shutil.copyfileobj(fileobj, handle)
    if member.mode & 0o111:
        target.chmod(target.stat().st_mode | 0o111)
    return total_bytes
