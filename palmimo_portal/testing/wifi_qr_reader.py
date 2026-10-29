from __future__ import annotations

import json
import socket
from pathlib import Path
from typing import Any


class FakeWifiQrReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self._listener.settimeout(2)
        self._connection: socket.socket | None = None
        self._pending = bytearray()

    def __enter__(self) -> FakeWifiQrReader:
        self._listener.bind(str(self.path))
        self._listener.listen(1)
        return self

    def _client(self) -> socket.socket:
        if self._connection is None:
            self._connection, _ = self._listener.accept()
            self._connection.settimeout(2)
        return self._connection

    def send(self, message: dict[str, Any]) -> None:
        self.send_raw(json.dumps(message, ensure_ascii=False).encode("utf-8") + b"\n")

    def send_raw(self, data: bytes) -> None:
        self._client().sendall(data)

    def receive(self) -> dict[str, Any]:
        while b"\n" not in self._pending:
            chunk = self._client().recv(4096)
            if not chunk:
                raise ConnectionError("Portal disconnected")
            self._pending.extend(chunk)
        line, _, rest = self._pending.partition(b"\n")
        self._pending = bytearray(rest)
        return dict(json.loads(line))

    def disconnect(self) -> None:
        client = self._client()
        client.shutdown(socket.SHUT_RDWR)
        client.close()

    def __exit__(self, *args: object) -> None:
        if self._connection is not None:
            self._connection.close()
        self._listener.close()
        self.path.unlink(missing_ok=True)
