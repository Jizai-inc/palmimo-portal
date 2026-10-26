"""Isolated hardware owner for Wi-Fi QR setup; no Portal imports are allowed."""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import fcntl
import importlib
import json
import logging
import os
import pwd
import signal
import socket
import stat
import struct
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any


LOG = logging.getLogger("wifi-qr-reader")
MAX_LINE = 32 * 1024
REASONS = frozenset(
    {"not_wifi", "unsupported_security", "hidden_network", "invalid_ssid", "invalid_psk", "busy", "connect_failed"}
)


class DeviceUnavailableError(Exception):
    pass


class DependencyUnavailableError(Exception):
    pass


class ReservationUnavailableError(Exception):
    def __init__(self, resource: str, holder: dict[str, Any] | None = None) -> None:
        super().__init__("Hardware reservation unavailable")
        self.resource = resource
        self.holder = holder or {}


@contextmanager
def reserve_resource(resource: str, directory: Path = Path("/run/lock/palmimo")) -> Iterator[None]:
    """Share the SDK's flock inode, permissions and holder metadata; never unlink it."""
    fd: int | None = None
    try:
        path = directory / f"{resource}.lock"
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        except FileNotFoundError:
            temporary_fd, temporary = tempfile.mkstemp(prefix=".palmimo-lock-", dir=directory)
            try:
                os.fchmod(temporary_fd, 0o666)
                with suppress(FileExistsError):
                    os.link(temporary, path)
            finally:
                os.close(temporary_fd)
                os.unlink(temporary)
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ReservationUnavailableError(resource)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EAGAIN, errno.EACCES}:
                raise
            holder: dict[str, Any] = {}
            with suppress(OSError, ValueError):
                metadata = json.loads(os.pread(fd, 65536, 0))
                if isinstance(metadata, dict):
                    holder = {key: metadata.get(key) for key in ("pid", "user", "app")}
            raise ReservationUnavailableError(resource, holder) from None
        metadata = {
            "pid": os.getpid(),
            "user": pwd.getpwuid(os.geteuid()).pw_name,
            "app": "palmimo-wifi-qr",
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        with os.fdopen(os.dup(fd), "w", encoding="utf-8") as stream:
            stream.seek(0)
            json.dump(metadata, stream)
            stream.truncate()
    except (OSError, KeyError):
        if fd is not None:
            os.close(fd)
        raise ReservationUnavailableError(resource) from None
    except BaseException:
        if fd is not None:
            os.close(fd)
        raise
    try:
        yield
    finally:
        os.close(fd)


def load_dependencies() -> None:
    try:
        importlib.import_module("zbar")
        importlib.import_module("serial")
        library = ctypes.util.find_library("turbojpeg")
        if library is None:
            raise DependencyUnavailableError
        ctypes.CDLL(library)
    except (ImportError, OSError):
        raise DependencyUnavailableError("Required hardware dependency unavailable") from None


class Camera:
    def __init__(self) -> None:
        raise DeviceUnavailableError("Camera backend unavailable")

    def read(self) -> Any | None:
        """Return a frame without waiting, or None when no frame is ready."""
        return None

    def close(self) -> None:
        pass


class Decoder:
    def decode(self, frame: Any) -> list[str]:
        return []


class Display:
    mode = "expr"

    def __init__(self) -> None:
        raise DeviceUnavailableError("Display backend unavailable")

    def screen(self, state: str, fields: dict[str, Any]) -> None:
        pass

    def live(self, frame: Any) -> None:
        pass

    def ping(self) -> None:
        pass

    def close(self) -> None:
        """Restore EXPR SLEEPING and release the device, including after partial setup."""


def peer_uid(connection: socket.socket) -> int:
    option = getattr(socket, "SO_PEERCRED", None)
    if option is None:
        raise OSError("Kernel peer credentials unavailable")
    credentials = connection.getsockopt(socket.SOL_SOCKET, option, struct.calcsize("3i"))
    return struct.unpack("3i", credentials)[1]


class Reader:
    def __init__(
        self,
        runtime_directory: Path,
        allowed_uid: int,
        *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        reserve: Callable[..., Any] = reserve_resource,
        load_dependencies: Callable[[], None] = load_dependencies,
        camera_factory: Callable[[], Any] = Camera,
        decoder_factory: Callable[[], Any] = Decoder,
        display_factory: Callable[[], Any] = Display,
        peer_uid_of: Callable[[socket.socket], int] = peer_uid,
    ) -> None:
        self.socket_path = runtime_directory / "control.sock"
        self.allowed_uid = allowed_uid
        self.clock = clock
        self.sleep = sleep
        self.reserve = reserve
        self.load_dependencies = load_dependencies
        self.camera_factory = camera_factory
        self.decoder_factory = decoder_factory
        self.display_factory = display_factory
        self.peer_uid_of = peer_uid_of
        self.stack = ExitStack()
        self.listener: socket.socket | None = None
        self.client: socket.socket | None = None
        self.camera: Any = None
        self.decoder: Any = None
        self.display: Any = None
        self.started_at = 0.0
        self.checking_since = 0.0
        self.last_ping = 0.0
        self.state = "scanning"
        self.last_text: str | None = None
        self.absent_frames = 0
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.discarding_line = False
        self.stopped = False
        self.exit_code = 0
        self.started = False

    def _cleanup(self, callback: Callable[[], Any]) -> None:
        try:
            callback()
        except Exception:
            LOG.warning("Hardware cleanup failed")

    def start(self) -> None:
        if self.started:
            return
        self.started = True
        self.started_at = self.clock()
        self.listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.stack.callback(self.listener.close)
        self.listener.bind(str(self.socket_path))
        self.stack.callback(self.socket_path.unlink, missing_ok=True)
        self.socket_path.chmod(0o666)
        self.listener.listen(4)
        self.listener.setblocking(False)
        for resource in ("camera", "display"):
            self.stack.enter_context(self.reserve(resource))
        try:
            self.load_dependencies()
        except (ImportError, OSError):
            raise DependencyUnavailableError("Required hardware dependency unavailable") from None
        self.camera = self.camera_factory()
        self.stack.callback(self._cleanup, self.camera.close)
        self.decoder = self.decoder_factory()
        self.display = self.display_factory()
        self.stack.callback(self._cleanup, self.display.close)
        self._screen("scanning", {})
        self.last_ping = self.clock()

    def _screen(self, state: str, fields: dict[str, Any]) -> None:
        self.state = state
        if state == "checking":
            self.checking_since = self.clock()
        self.display.screen(state, fields)

    def _send(self, message: dict[str, Any]) -> None:
        line = json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")
        if len(line) <= MAX_LINE:
            self.outgoing.extend(line + b"\n")

    def _accept(self) -> None:
        assert self.listener is not None
        for _ in range(16):
            try:
                connection, _ = self.listener.accept()
            except BlockingIOError:
                break
            try:
                allowed = self.peer_uid_of(connection) == self.allowed_uid
            except OSError:
                allowed = False
            if not allowed or self.client is not None:
                connection.close()
                continue
            self.client = connection
            self.stack.callback(connection.close)
            connection.setblocking(False)
            self._send({"type": "hello", "proto": 1, "display": self.display.mode})

    def _line(self, line: bytes) -> None:
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError):
            LOG.warning("Invalid control message discarded")
            return
        if not isinstance(message, dict) or message.get("type") != "screen":
            return
        state = message.get("state")
        if not isinstance(state, str):
            return
        fields: dict[str, Any] = {}
        if state in {"rejected", "failed"}:
            reason = message.get("reason")
            if not isinstance(reason, str) or reason not in REASONS:
                return
            fields["reason"] = reason
        elif state in {"connecting", "connected"}:
            key = "ssid" if state == "connecting" else "hostname"
            value = message.get(key)
            if not isinstance(value, str):
                return
            fields[key] = value
        elif state not in {"scanning", "checking"}:
            return
        self._screen(state, fields)

    def _receive(self) -> None:
        assert self.client is not None
        try:
            chunk = self.client.recv(65536)
        except BlockingIOError:
            return
        if not chunk:
            self.stop()
            return
        for byte in chunk:
            if byte == 10:
                if not self.discarding_line:
                    self._line(bytes(self.incoming))
                self.incoming.clear()
                self.discarding_line = False
            elif not self.discarding_line:
                self.incoming.append(byte)
                if len(self.incoming) > MAX_LINE:
                    self.incoming.clear()
                    self.discarding_line = True

    def _flush(self) -> None:
        if self.client is not None and self.outgoing:
            try:
                sent = self.client.send(self.outgoing)
            except BlockingIOError:
                return
            del self.outgoing[:sent]

    def _frame(self) -> None:
        frame = self.camera.read()
        if frame is None:
            return
        texts = self.decoder.decode(frame)
        if self.last_text is not None:
            self.absent_frames = 0 if self.last_text in texts else self.absent_frames + 1
            if self.absent_frames >= 30:
                self.last_text = None
        text = next((text for text in texts if text != self.last_text), None)
        if text is not None and self.client is not None:
            self._screen("checking", {})
            self.last_text = text
            self.absent_frames = 0
            self._send({"type": "qr", "text": text})
        elif self.display.mode == "frame":
            self.display.live(frame)

    def step(self) -> bool:
        if self.stopped:
            return False
        self._accept()
        try:
            if self.client is not None:
                self._receive()
            if self.stopped:
                return False
            now = self.clock()
            if self.client is None and now - self.started_at >= 30:
                self.stop()
                return False
            if self.state == "checking" and now - self.checking_since >= 15:
                self._screen("scanning", {})
            if self.display.mode == "frame" and now - self.last_ping >= 2:
                self.display.ping()
                self.last_ping = now
            if self.state == "scanning":
                self._frame()
            self._flush()
        except (BrokenPipeError, ConnectionResetError):
            self.stop()
        return not self.stopped

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        self.stack.close()
        self.incoming.clear()
        self.outgoing.clear()
        self.last_text = None

    def run(self) -> int:
        try:
            if not self.stopped:
                self.start()
            while self.step():
                self.sleep(0.01)
        except ReservationUnavailableError as error:
            LOG.warning("Reservation unavailable resource=%s holder=%s", error.resource, error.holder)
            self.exit_code = 75
        except DependencyUnavailableError:
            LOG.warning("Required hardware dependency unavailable")
            self.exit_code = 69
        except DeviceUnavailableError:
            LOG.warning("Camera or display unavailable")
            self.exit_code = 78
        except Exception:
            # Decoder and JSON exceptions may embed credentials, including in chained tracebacks.
            LOG.error("Reader operation failed")
            self.exit_code = 1
        finally:
            self.close()
        return self.exit_code


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        runtime_directory = Path(os.environ.get("RUNTIME_DIRECTORY", "/run/palmimo-wifi-qr"))
        allowed_uid = pwd.getpwnam(os.environ["PALMIMO_WIFI_QR_PEER_USER"]).pw_uid
    except KeyError:
        LOG.error("Portal peer user is not configured or does not exist")
        return 1
    reader = Reader(runtime_directory, allowed_uid)
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: reader.stop())
    try:
        return reader.run()
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
