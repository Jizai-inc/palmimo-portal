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

import codecs
import io
import json
import logging
import os
import selectors
import shutil
import subprocess
import tarfile
import time
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
    DisplayFirmwareResult,
    DisplayFirmwareState,
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
DISPLAY_FIRMWARE_SUBPROCESS_TIMEOUT_SECONDS = 660.0

#: `install.sh verify`'s own exit-code contract: 0 = clean, 1 = differences found (still a
#: successful run -- the diffs are its stdout), >=2 = the command itself failed.
_VERIFY_SUCCESS_CODES = (0, 1)

#: Mirrors adapters/static_asset.py's gzip-bomb guard -- the platform bundle
#: (install.sh, manifest.json, a handful of unit/polkit/tmpfiles files) is small.
MEMBER_MAX_BYTES = 20 * 1024 * 1024
TOTAL_MAX_BYTES = 200 * 1024 * 1024


def _run_process(
    argv: list[str],
    *,
    capture_output: bool,
    text: bool,
    timeout: float,
    on_stderr: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    if on_stderr is None:
        return subprocess.run(argv, capture_output=capture_output, text=text, timeout=timeout)
    output = {"stdout": bytearray(), "stderr": bytearray()}
    pending = ""
    with subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True) as process:
        assert isinstance(process.stdout, io.TextIOWrapper)
        assert process.stderr is not None
        encoding = process.stdout.encoding
        # Progress decoding must not abort an in-flight firmware write.
        decoder = io.IncrementalNewlineDecoder(codecs.getincrementaldecoder(encoding)(errors="replace"), translate=True)
        deadline = time.monotonic() + timeout
        try:
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ, "stdout")
                selector.register(process.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map():
                    remaining = deadline - time.monotonic()
                    if remaining <= 0:
                        raise subprocess.TimeoutExpired(argv, timeout)
                    for key, _ in selector.select(remaining):
                        chunk = os.read(key.fd, 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        output[key.data].extend(chunk)
                        if key.data == "stderr":
                            pending += decoder.decode(chunk, final=not chunk)
                            lines = pending.split("\n")
                            pending = lines.pop()
                            for line in lines:
                                on_stderr(line)
                            if not chunk and pending:
                                on_stderr(pending)
                                pending = ""
                process.wait(timeout=max(0, deadline - time.monotonic()))
        except BaseException:
            process.kill()
            process.wait()
            raise
    return subprocess.CompletedProcess(
        argv,
        process.returncode,
        stdout=output["stdout"].decode(encoding).replace("\r\n", "\n").replace("\r", "\n"),
        stderr=output["stderr"].decode(encoding).replace("\r\n", "\n").replace("\r", "\n"),
    )


@dataclass
class SudoPlatformPort(PlatformPort):
    """Reads ``installed.json`` directly (no privilege needed) and runs ``install.sh`` via ``sudo``."""

    installed_path: Path
    sudo_bin: str = "sudo"
    runner: object = field(default=_run_process)

    boot_id_path: Path = Path("/proc/sys/kernel/random/boot_id")
    display_firmware_tool: Path = Path("/usr/local/libexec/palmimo/palmimo-display-firmware")
    display_firmware_state_path: Path = Path("/var/lib/palmimo/display-firmware/state.json")

    def read_boot_id(self) -> str | None:
        try:
            return self.boot_id_path.read_text(encoding="utf-8").strip() or None
        except (OSError, ValueError):
            return None

    def has_display_firmware_tool(self) -> bool:
        return self.display_firmware_tool.is_file()

    def ensure_display_firmware(self) -> DisplayFirmwareResult:
        process = self._run(
            "display_firmware",
            [str(self.display_firmware_tool), "ensure", "--json"],
            command_timeout=600.0,
            subprocess_timeout=DISPLAY_FIRMWARE_SUBPROCESS_TIMEOUT_SECONDS,
            success_codes=(0, 75, 1, 78),
        )
        output, stderr = process.stdout, process.stderr or ""
        try:
            if len(output.strip().splitlines()) != 1:
                raise ValueError("expected one JSON line")
            result = DisplayFirmwareResult.from_payload(json.loads(output))
            if result.result == "interrupted":
                raise ValueError("unexpected tool result")
            return result
        except (ValueError, TypeError) as error:
            detail = f"invalid tool output: {error}"
            if stderr.strip():
                detail += f": {stderr[-2000:].strip()}"
            raise PlatformCommandError("display_firmware", detail) from error

    def read_display_firmware_state(self) -> DisplayFirmwareState | None:
        try:
            payload: Any = json.loads(self.display_firmware_state_path.read_text(encoding="utf-8"))
            return DisplayFirmwareState.from_payload(payload)
        except (OSError, ValueError):
            return None

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
        ).stdout
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
    ) -> subprocess.CompletedProcess[str]:
        # `-n`: never fall back to a password prompt no one can answer. `timeout -k 5 <secs>`:
        # a bounded budget enforced on the command itself, independent of (and smaller than) the
        # subprocess-level timeout below, which exists only to catch `timeout`/`sudo` itself hanging.
        full_argv = [self.sudo_bin, "-n", "timeout", "-k", "5", str(int(command_timeout)), *argv]
        options: dict[str, Any] = {"capture_output": True, "text": True, "timeout": subprocess_timeout}
        if step == "display_firmware":
            options["on_stderr"] = lambda line: logger.info("display-firmware: %s", line)
        try:
            result = self.runner(full_argv, **options)  # type: ignore[operator]
        except subprocess.TimeoutExpired as error:
            raise PlatformCommandError(step, f"timed out after {subprocess_timeout:g}s") from error
        except OSError as error:
            raise PlatformCommandError(step, f"failed to start: {error}") from error
        if result.returncode not in success_codes:
            tail = (result.stderr or "").strip()
            raise PlatformCommandError(step, tail or f"exited {result.returncode}")
        return result


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
