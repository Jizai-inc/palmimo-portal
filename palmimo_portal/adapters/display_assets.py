from __future__ import annotations

import csv
import json
import os
import re
import stat
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal

from palmimo_portal.ports import (
    DisplayAssetConversion,
    DisplayAssetMessage,
    DisplayFirmwareNeed,
    DisplayToolError,
)


def _read_regular(path: Path, limit: int | None = None) -> bytes:
    parts = path.absolute().parts
    descriptor = os.open(parts[0], os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[1:-1]:
            if part == "..":
                raise ValueError("parent traversal is not allowed")
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
            os.close(descriptor)
            descriptor = child
        with os.fdopen(
            os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=descriptor), "rb"
        ) as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise ValueError("expected a regular file")
            data = stream.read() if limit is None else stream.read(limit + 1)
            if limit is not None and len(data) > limit:
                raise ValueError("file exceeds size limit")
            return data
    finally:
        os.close(descriptor)


@dataclass
class AppEnvironment:
    def read_display_firmware_need(self, venv: Path) -> DisplayFirmwareNeed:
        try:
            distributions = list(venv.glob("lib/python3.*/site-packages/palmimo_sdk-*.dist-info"))
            if not distributions:
                return "none"
            if len(distributions) != 1:
                return "1"
            distribution = distributions[0]
            rows = csv.reader(_read_regular(distribution / "RECORD").decode("utf-8").splitlines())
            if sum(bool(row) and row[0] == "palmimo_sdk/display_firmware.json" for row in rows) != 1:
                return "1"
            payload = json.loads(_read_regular(distribution.parent / "palmimo_sdk/display_firmware.json", 4096))
            if (
                isinstance(payload, dict)
                and type(payload.get("schema")) is int
                and payload["schema"] == 1
                and type(payload.get("protocol")) is int
                and payload["protocol"] in (1, 2)
            ):
                return "2" if payload["protocol"] == 2 else "1"
        except (OSError, ValueError, csv.Error):
            pass
        return "1"


@dataclass
class StandardAssets:
    directory: Path = Path("/usr/local/share/palmimo/display-assets")

    def list_standard_names(self) -> tuple[str, ...]:
        try:
            names = []
            for path in self.directory.iterdir():
                if path.suffix != ".bin":
                    continue
                name = path.stem
                if (
                    re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,39}", name) is None
                    or name == "include"
                    or not stat.S_ISREG(path.lstat().st_mode)
                ):
                    raise ValueError(f"invalid standard asset: {path.name}")
                names.append(name)
            return tuple(sorted(names))
        except (OSError, ValueError) as error:
            raise DisplayToolError(str(error)) from error

    def read_catalog(self) -> dict[str, Any]:
        try:
            payload = json.loads(_read_regular(self.directory / "catalog.json"))
            if not isinstance(payload, dict):
                raise ValueError("invalid display asset catalog")
            return payload
        except (OSError, ValueError) as error:
            raise DisplayToolError(str(error)) from error


def _messages(payload: object) -> tuple[DisplayAssetMessage, ...]:
    if not isinstance(payload, list):
        raise ValueError("invalid converter diagnostics")
    messages = []
    for entry in payload:
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("code"), str)
            or not isinstance(entry.get("message"), str)
            or (entry.get("path") is not None and not isinstance(entry["path"], str))
        ):
            raise ValueError("invalid converter diagnostic")
        messages.append(DisplayAssetMessage(entry["code"], entry["message"], entry.get("path")))
    return tuple(messages)


@dataclass
class DisplayAssetConverter:
    tool: Path = Path("/usr/local/libexec/palmimo/palmimo-display-assets")
    runner: object = field(default=subprocess.run)

    def convert(self, src: Path, dst: Path) -> DisplayAssetConversion:
        return self._run(["convert", str(src), "-o", str(dst), "--json"])

    def inspect(self, path: Path) -> DisplayAssetConversion:
        return self._run(["inspect", str(path), "--json"])

    def _run(self, arguments: list[str]) -> DisplayAssetConversion:
        try:
            process = self.runner(  # type: ignore[operator]
                [str(self.tool), *arguments], capture_output=True, text=True, timeout=60.0
            )
            if process.returncode not in (0, 2):
                raise ValueError(process.stderr or f"converter exited {process.returncode}")
            if len(process.stdout.strip().splitlines()) != 1:
                raise ValueError("expected one JSON line")
            payload = json.loads(process.stdout)
            if not isinstance(payload, dict) or type(payload.get("ok")) is not bool:
                raise ValueError("invalid converter result")
            ok = payload["ok"]
            if ok != (process.returncode == 0):
                raise ValueError("converter exit code disagrees with result")
            kind: Literal["vector", "image", "gif"] | None = None
            size = crc = None
            params = {}
            if ok:
                if payload.get("kind") not in ("vector", "image", "gif"):
                    raise ValueError("invalid converter kind")
                kind = payload["kind"]
                size, crc = payload.get("bytes"), payload.get("crc32")
                if (
                    type(size) is not int
                    or size < 0
                    or not isinstance(crc, str)
                    or not re.fullmatch(r"[0-9a-f]{8}", crc)
                ):
                    raise ValueError("invalid converter metadata")
                if not isinstance(payload.get("params"), dict):
                    raise ValueError("invalid converter parameters")
                for sid, entry in payload["params"].items():
                    if not isinstance(entry, dict) or type(entry.get("dim")) is not int or entry["dim"] <= 0:
                        raise ValueError("invalid converter parameter dimension")
                    params[sid] = entry["dim"]
            return DisplayAssetConversion(
                ok,
                kind,
                size,
                crc,
                params,
                _messages(payload.get("warnings", [])),
                _messages(payload.get("errors", [])),
            )
        except (OSError, ValueError, subprocess.TimeoutExpired) as error:
            raise DisplayToolError(str(error)) from error
