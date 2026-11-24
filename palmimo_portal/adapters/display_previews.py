from __future__ import annotations

import os
import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

from palmimo_portal.adapters.display_assets import SOURCE_MAX_BYTES, SOURCE_MEDIA_TYPES, _read_regular, valid_asset_name
from palmimo_portal.ports import DisplayAssetConverterPort, DisplayAssetPreview


PREVIEW_TTL_SECONDS = 600


@dataclass
class DisplayAssetPreviews:
    directory: Path
    converter: DisplayAssetConverterPort
    monotonic: Callable[[], float] = time.monotonic
    initialize: bool = True
    _entries: dict[str, tuple[DisplayAssetPreview, float, threading.Timer]] = field(default_factory=dict, init=False)
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False)
    _initialized: bool = field(default=False, init=False)
    _leased: dict[str, int] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.initialize:
            self._prepare()

    def _prepare(self) -> None:
        if self._initialized:
            return
        if self.directory.is_symlink():
            raise ValueError("preview directory cannot be a symlink")
        self.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        for path in self.directory.iterdir():
            if path.is_dir() and not path.is_symlink():
                shutil.rmtree(path)
            else:
                path.unlink()
        for path in self.directory.parent.iterdir():
            if re.fullmatch(r"[0-9a-f]{32}\.bin", path.name):
                path.unlink()
        if self.originals.is_dir() and not self.originals.is_symlink():
            for path in self.originals.iterdir():
                if re.match(r"\.[0-9a-f]{32}(?:-|\.)", path.name):
                    path.unlink()
        self._initialized = True

    def create(self, name: str, content: bytes, suffix: str) -> DisplayAssetPreview:
        suffix = suffix.lower()
        if suffix not in SOURCE_MEDIA_TYPES:
            raise ValueError("unsupported source extension")
        with self._lock:
            self._prepare()
        token = secrets.token_hex(16)
        destination = self.directory / f"{token}.bin"
        original = self.directory / f"{token}{suffix}"
        try:
            original.write_bytes(content)
            conversion = self.converter.convert(original, destination)
            preview = DisplayAssetPreview(token, name, destination, conversion, original)
            if conversion.ok:
                timer = threading.Timer(PREVIEW_TTL_SECONDS, self.delete, args=(token,))
                timer.daemon = True
                with self._lock:
                    self._entries[token] = (preview, self.monotonic() + PREVIEW_TTL_SECONDS, timer)
                    timer.start()
            else:
                destination.unlink(missing_ok=True)
                original.unlink(missing_ok=True)
            return preview
        except BaseException:
            destination.unlink(missing_ok=True)
            original.unlink(missing_ok=True)
            raise

    @property
    def originals(self) -> Path:
        return self.directory.parent / "originals"

    def read_original(self, name: str) -> tuple[bytes, str] | None:
        if not valid_asset_name(name):
            return None
        for suffix, media_type in SOURCE_MEDIA_TYPES.items():
            try:
                return _read_regular(self.originals / f"{name}{suffix}", SOURCE_MAX_BYTES), media_type
            except (OSError, ValueError):
                continue
        return None

    def delete_original(self, name: str) -> None:
        if not valid_asset_name(name):
            raise ValueError("invalid asset name")
        for suffix in SOURCE_MEDIA_TYPES:
            (self.originals / f"{name}{suffix}").unlink(missing_ok=True)

    @contextmanager
    def save_original(self, name: str, preview: DisplayAssetPreview) -> Iterator[None]:
        if not valid_asset_name(name):
            raise ValueError("invalid asset name")
        if self.originals.is_symlink():
            raise ValueError("originals directory cannot be a symlink")
        self.originals.mkdir(mode=0o700, parents=True, exist_ok=True)
        suffix = preview.source_path.suffix
        destination = self.originals / f"{name}{suffix}"
        temporary = self.originals / f".{preview.token}{suffix}"
        backups: list[tuple[Path, Path]] = []
        overridden = False
        try:
            temporary.write_bytes(_read_regular(preview.source_path, SOURCE_MAX_BYTES))
            # A process death during the override must not expose a stale original.
            for extension in SOURCE_MEDIA_TYPES:
                current = self.originals / f"{name}{extension}"
                if current.exists() or current.is_symlink():
                    backup = self.originals / f".{preview.token}-{name}{extension}"
                    os.replace(current, backup)
                    backups.append((current, backup))
            yield
            overridden = True
            os.replace(temporary, destination)
        finally:
            for current, backup in backups:
                if overridden:
                    backup.unlink(missing_ok=True)
                else:
                    os.replace(backup, current)
            temporary.unlink(missing_ok=True)

    def _read(self, name: str, token: str) -> DisplayAssetPreview | None:
        if re.fullmatch(r"[0-9a-f]{32}", token) is None:
            return None
        entry = self._entries.get(token)
        if entry is None:
            return None
        preview, deadline, _ = entry
        if self.monotonic() >= deadline or not preview.path.is_file() or preview.path.is_symlink():
            self._delete(token)
            return None
        return preview if preview.name == name else None

    @contextmanager
    def open(self, name: str, token: str) -> Iterator[DisplayAssetPreview | None]:
        with self._lock:
            preview = self._read(name, token)
            if preview is not None:
                self._leased[token] = self._leased.get(token, 0) + 1
        try:
            yield preview
        finally:
            with self._lock:
                if preview is not None:
                    self._leased[token] -= 1
                    if self._leased[token] == 0:
                        del self._leased[token]
                entry = self._entries.get(token)
                if entry is not None and self.monotonic() >= entry[1]:
                    self._delete(token)

    def delete(self, token: str) -> None:
        with self._lock:
            self._delete(token)

    def _delete(self, token: str) -> None:
        if token in self._leased:
            return
        entry = self._entries.pop(token, None)
        if entry is not None:
            preview, _, timer = entry
            timer.cancel()
            preview.path.unlink(missing_ok=True)
            preview.source_path.unlink(missing_ok=True)
