from __future__ import annotations

import json
import logging
import socket
import threading
from collections.abc import Callable
from contextlib import suppress
from pathlib import Path
from typing import Any


logger = logging.getLogger("palmimo_portal")
MAX_LINE_BYTES = 32 * 1024
CONTROL_SOCKET = Path("/run/palmimo-wifi-qr/control.sock")


class WifiQrSocketClient:
    """Callbacks run on the receive thread; close() may be called from a callback.

    Wire contract: see doc/wifi-qr-reader.md.
    """

    def __init__(
        self,
        on_message: Callable[[dict[str, Any]], None],
        *,
        on_disconnect: Callable[[], None] | None = None,
        path: Path = CONTROL_SOCKET,
    ) -> None:
        self._on_message = on_message
        self._on_disconnect = on_disconnect
        self._path = path
        self._socket: socket.socket | None = None
        self._thread: threading.Thread | None = None
        self._send_lock = threading.Lock()

    def connect(self) -> None:
        if self._thread is not None:
            raise RuntimeError("Wi-Fi QR client has already been connected")
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(1)
        try:
            connection.connect(str(self._path))
        except OSError:
            connection.close()
            raise ConnectionError("Wi-Fi QR socket connection failed") from None
        self._socket = connection
        self._thread = threading.Thread(target=self._receive, args=(connection,), name="wifi-qr-socket", daemon=True)
        self._thread.start()

    def send(self, message: dict[str, Any]) -> bool:
        try:
            line = json.dumps(message, ensure_ascii=False).encode("utf-8")
        except (ValueError, TypeError, UnicodeError, RecursionError):
            raise ValueError("Invalid Wi-Fi QR outgoing message") from None
        if len(line) > MAX_LINE_BYTES:
            return False
        with self._send_lock:
            if self._socket is None:
                raise ConnectionError("Wi-Fi QR socket is disconnected")
            try:
                self._socket.sendall(line + b"\n")
            except OSError:
                raise ConnectionError("Wi-Fi QR socket send failed") from None
        return True

    def close(self) -> None:
        with self._send_lock:
            connection, self._socket = self._socket, None
            if connection is not None:
                with suppress(OSError):
                    connection.shutdown(socket.SHUT_RDWR)
                connection.close()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)

    def _deliver(self, line: bytes) -> None:
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeError, RecursionError):
            logger.warning("wifi-qr: invalid socket message")
            return
        if not isinstance(message, dict):
            return
        kind = message.get("type")
        if kind == "qr":
            if not isinstance(message.get("text"), str):
                return
        elif kind == "hello":
            if type(message.get("proto")) is not int or message["proto"] != 1:
                return
            if message.get("display") not in ("frame", "expr"):
                return
        else:
            return
        try:
            self._on_message(message)
        except Exception:
            logger.error("wifi-qr: message callback failed")

    def _receive(self, connection: socket.socket) -> None:
        pending = bytearray()
        dropping = False
        try:
            while True:
                try:
                    data = connection.recv(4096)
                except TimeoutError:
                    continue
                if not data:
                    break
                pieces = data.split(b"\n")
                for index, piece in enumerate(pieces):
                    if not dropping:
                        if len(pending) + len(piece) > MAX_LINE_BYTES:
                            dropping = True
                            pending.clear()
                        else:
                            pending.extend(piece)
                    if index < len(pieces) - 1:
                        if not dropping:
                            self._deliver(bytes(pending))
                        pending.clear()
                        dropping = False
        except OSError:
            pass
        finally:
            with self._send_lock:
                if self._socket is connection:
                    self._socket = None
                connection.close()
            if self._on_disconnect is not None:
                try:
                    self._on_disconnect()
                except Exception:
                    logger.error("wifi-qr: disconnect callback failed")
