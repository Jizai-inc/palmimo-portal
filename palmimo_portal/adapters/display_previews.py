from __future__ import annotations

import re
import secrets
import shutil
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory

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
        self._initialized = True

    def create(self, name: str, content: bytes, suffix: str) -> DisplayAssetPreview:
        with self._lock:
            self._prepare()
        token = secrets.token_hex(16)
        destination = self.directory / f"{token}.bin"
        try:
            with TemporaryDirectory(dir=self.directory) as work:
                source = Path(work) / f"source{suffix}"
                source.write_bytes(content)
                conversion = self.converter.convert(source, destination)
            preview = DisplayAssetPreview(token, name, destination, conversion)
            if conversion.ok:
                timer = threading.Timer(PREVIEW_TTL_SECONDS, self.delete, args=(token,))
                timer.daemon = True
                with self._lock:
                    self._entries[token] = (preview, self.monotonic() + PREVIEW_TTL_SECONDS, timer)
                    timer.start()
            else:
                destination.unlink(missing_ok=True)
            return preview
        except BaseException:
            destination.unlink(missing_ok=True)
            raise

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
