"""Isolated hardware owner for Wi-Fi QR setup; no Portal imports are allowed."""

from __future__ import annotations

import base64
import ctypes
import ctypes.util
import errno
import fcntl
import importlib
import json
import logging
import mmap
import os
import pwd
import signal
import socket
import stat
import struct
import tempfile
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, ClassVar


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


# Layouts and ioctl numbers follow Linux UAPI <linux/videodev2.h> on 64-bit Linux.
class V4L2Pixels(ctypes.Structure):
    _fields_: ClassVar[Any] = [
        (name, ctypes.c_uint32)
        for name in (
            "width",
            "height",
            "pixelformat",
            "field",
            "bytesperline",
            "sizeimage",
            "colorspace",
            "priv",
            "flags",
            "ycbcr_enc",
            "quantization",
            "xfer_func",
        )
    ]


class V4L2Formats(ctypes.Union):
    _fields_: ClassVar[Any] = [("pix", V4L2Pixels), ("raw", ctypes.c_uint8 * 200), ("alignment", ctypes.c_uint64)]


class V4L2Format(ctypes.Structure):
    _fields_: ClassVar[Any] = [("type", ctypes.c_uint32), ("fmt", V4L2Formats)]


class V4L2RequestBuffers(ctypes.Structure):
    _fields_: ClassVar[Any] = [
        ("count", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("capabilities", ctypes.c_uint32),
        ("flags", ctypes.c_uint8),
        ("reserved", ctypes.c_uint8 * 3),
    ]


class V4L2Timeval(ctypes.Structure):
    _fields_: ClassVar[Any] = [("seconds", ctypes.c_int64), ("microseconds", ctypes.c_int64)]


class V4L2Timecode(ctypes.Structure):
    _fields_: ClassVar[Any] = [
        ("type", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("frames", ctypes.c_uint8),
        ("seconds", ctypes.c_uint8),
        ("minutes", ctypes.c_uint8),
        ("hours", ctypes.c_uint8),
        ("userbits", ctypes.c_uint8 * 4),
    ]


class V4L2Memory(ctypes.Union):
    _fields_: ClassVar[Any] = [("offset", ctypes.c_uint32), ("userptr", ctypes.c_uint64), ("fd", ctypes.c_int32)]


class V4L2Buffer(ctypes.Structure):
    _fields_: ClassVar[Any] = [
        ("index", ctypes.c_uint32),
        ("type", ctypes.c_uint32),
        ("bytesused", ctypes.c_uint32),
        ("flags", ctypes.c_uint32),
        ("field", ctypes.c_uint32),
        ("timestamp", V4L2Timeval),
        ("timecode", V4L2Timecode),
        ("sequence", ctypes.c_uint32),
        ("memory", ctypes.c_uint32),
        ("m", V4L2Memory),
        ("length", ctypes.c_uint32),
        ("reserved2", ctypes.c_uint32),
        ("request_fd", ctypes.c_int32),
    ]


VIDIOC_S_FMT = 0xC0D05605
VIDIOC_REQBUFS = 0xC0145608
VIDIOC_QUERYBUF = 0xC0585609
VIDIOC_QBUF = 0xC058560F
VIDIOC_DQBUF = 0xC0585611
VIDIOC_STREAMON = 0x40045612
VIDIOC_STREAMOFF = 0x40045613


def video_ioctl(fd: int, request: int, argument: Any) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    libc.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
    libc.ioctl.restype = ctypes.c_int
    while libc.ioctl(fd, request, ctypes.byref(argument)) < 0:
        error = ctypes.get_errno()
        if error != errno.EINTR:
            raise OSError(error, "Camera ioctl failed")


def map_video_buffer(fd: int, length: int, offset: int) -> mmap.mmap:
    return mmap.mmap(fd, length, flags=mmap.MAP_SHARED, prot=mmap.PROT_READ | mmap.PROT_WRITE, offset=offset)


class Camera:
    def __init__(
        self,
        path: Path = Path("/dev/video0"),
        *,
        ioctl: Callable[..., None] = video_ioctl,
        map_buffer: Callable[..., Any] = map_video_buffer,
    ) -> None:
        self.ioctl = ioctl
        self.fd: int | None = None
        self.buffers: list[Any] = []
        self.streaming = False
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK | os.O_CLOEXEC)
            format_ = V4L2Format(type=1)
            format_.fmt.pix = V4L2Pixels(width=2592, height=1944, pixelformat=int.from_bytes(b"MJPG", "little"))
            self.ioctl(self.fd, VIDIOC_S_FMT, format_)
            pix = format_.fmt.pix
            if (pix.width, pix.height, pix.pixelformat) != (2592, 1944, int.from_bytes(b"MJPG", "little")):
                raise DeviceUnavailableError("Camera capture mode unavailable")
            request = V4L2RequestBuffers(count=4, type=1, memory=1)
            self.ioctl(self.fd, VIDIOC_REQBUFS, request)
            if not 1 <= request.count <= 32:
                raise DeviceUnavailableError("Camera capture buffers unavailable")
            for index in range(request.count):
                buffer = V4L2Buffer(index=index, type=1, memory=1)
                self.ioctl(self.fd, VIDIOC_QUERYBUF, buffer)
                self.buffers.append(map_buffer(self.fd, buffer.length, buffer.m.offset))
                self.ioctl(self.fd, VIDIOC_QBUF, buffer)
            self.ioctl(self.fd, VIDIOC_STREAMON, ctypes.c_int(1))
            self.streaming = True
        except BaseException as error:
            self.close()
            if isinstance(error, OSError) and error.errno in {errno.ENOENT, errno.ENODEV, errno.ENXIO}:
                raise DeviceUnavailableError("Camera unavailable") from None
            raise

    def read(self) -> bytes | None:
        """Drain queued frames without waiting; only copy the newest complete MJPEG buffer."""
        if self.fd is None:
            return None
        dequeued: list[V4L2Buffer] = []
        try:
            for _ in self.buffers:
                buffer = V4L2Buffer(type=1, memory=1)
                try:
                    self.ioctl(self.fd, VIDIOC_DQBUF, buffer)
                except BlockingIOError:
                    break
                dequeued.append(buffer)
            for buffer in reversed(dequeued):
                if buffer.index >= len(self.buffers) or buffer.bytesused > len(self.buffers[buffer.index]):
                    raise RuntimeError("Invalid camera buffer")
                if buffer.bytesused and not buffer.flags & 0x40:
                    return bytes(self.buffers[buffer.index][: buffer.bytesused])
            return None
        finally:
            for buffer in dequeued:
                self.ioctl(self.fd, VIDIOC_QBUF, buffer)

    def close(self) -> None:
        if self.fd is None:
            return
        if self.streaming:
            with suppress(OSError):
                self.ioctl(self.fd, VIDIOC_STREAMOFF, ctypes.c_int(1))
            self.streaming = False
        for buffer in self.buffers:
            if hasattr(buffer, "close"):
                buffer.close()
            else:
                buffer.release()
        self.buffers.clear()
        os.close(self.fd)
        self.fd = None


def preview_gray(pixels: bytes, width: int, height: int, *, size: int = 168) -> bytes:
    if width <= 0 or height <= 0 or size <= 0 or len(pixels) != width * height:
        raise ValueError("Invalid grayscale image")
    side = min(width, height)
    left, top = (width - side) // 2, (height - side) // 2
    columns = [left + x * side // size for x in range(size)]
    # The camera is mounted upside down; QR decoding itself is orientation independent.
    return bytes(pixels[(top + y * side // size) * width + x] for y in range(size) for x in columns)[::-1]


class TurboJPEG:
    def __init__(self, library: Any = None) -> None:
        self.library = library if library is not None else ctypes.CDLL("libturbojpeg.so.0")
        self.library.tjInitDecompress.argtypes = []
        self.library.tjInitDecompress.restype = ctypes.c_void_p
        self.library.tjDecompressHeader3.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong] + [
            ctypes.POINTER(ctypes.c_int)
        ] * 4
        self.library.tjDecompressHeader3.restype = ctypes.c_int
        self.library.tjDecompress2.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_ulong, ctypes.c_void_p] + [
            ctypes.c_int
        ] * 5
        self.library.tjDecompress2.restype = ctypes.c_int
        self.library.tjDestroy.argtypes = [ctypes.c_void_p]
        self.library.tjDestroy.restype = ctypes.c_int
        self.handle = self.library.tjInitDecompress()
        if not self.handle:
            raise RuntimeError("JPEG decoder initialization failed")

    def decode(self, jpeg: bytes, denominator: int) -> tuple[bytes, int, int]:
        width, height, subsampling, colorspace = (ctypes.c_int() for _ in range(4))
        if (
            self.library.tjDecompressHeader3(
                self.handle,
                jpeg,
                len(jpeg),
                ctypes.byref(width),
                ctypes.byref(height),
                ctypes.byref(subsampling),
                ctypes.byref(colorspace),
            )
            != 0
        ):
            raise ValueError("Invalid camera JPEG")
        if not 0 < width.value <= 2592 or not 0 < height.value <= 1944 or denominator not in {2, 4}:
            raise ValueError("Invalid camera JPEG dimensions or scale")
        w, h = (width.value + denominator - 1) // denominator, (height.value + denominator - 1) // denominator
        output = ctypes.create_string_buffer(w * h)
        if self.library.tjDecompress2(self.handle, jpeg, len(jpeg), output, w, 0, h, 6, 0) != 0:
            raise ValueError("Camera JPEG decode failed")
        return output.raw, w, h

    def close(self) -> None:
        if self.handle:
            self.library.tjDestroy(self.handle)
            self.handle = None


class Decoder:
    def __init__(self, *, jpeg: Any = None, zbar_module: Any = None) -> None:
        self.zbar = zbar_module if zbar_module is not None else importlib.import_module("zbar")
        self.jpeg = jpeg if jpeg is not None else TurboJPEG()
        try:
            self.scanner = self.zbar.ImageScanner()
            self.scanner.parse_config("disable")
            self.scanner.parse_config("qrcode.enable")
        except BaseException:
            self.jpeg.close()
            raise
        self.preview: bytes | None = None

    def decode(self, frame: bytes) -> list[str]:
        texts: list[str] = []
        for denominator in (4, 2):
            pixels, width, height = self.jpeg.decode(frame, denominator)
            if denominator == 4:
                self.preview = preview_gray(pixels, width, height)
            image = self.zbar.Image(width, height, "Y800", pixels)
            self.scanner.scan(image)
            for symbol in image:
                raw = symbol.data
                text = raw.decode("utf-8") if isinstance(raw, bytes) else raw
                if isinstance(text, str) and text not in texts:
                    texts.append(text)
        return texts

    def close(self) -> None:
        self.preview = None
        self.jpeg.close()


class DisplayProtocolError(Exception):
    pass


class TransferCancelledError(Exception):
    pass


def parse_power(line: bytes) -> bool:
    fields = line.split()
    powers = [field for field in fields[1:] if field.startswith(b"power=")]
    if not fields or fields[0] != b"STAT" or len(powers) != 1 or powers[0] not in {b"power=ON", b"power=OFF"}:
        raise DisplayProtocolError("Display power state unavailable")
    return powers[0] == b"power=ON"


class FrameClient:
    def __init__(self, port: Any, *, clock: Callable[[], float] = time.monotonic, gate: Any = None) -> None:
        self.port = port
        self.clock = clock
        self.gate = gate if gate is not None else threading.RLock()
        self.partial = bytearray()
        self.original_power: bool | None = None
        self.mode = "expr"
        self.powered_on = False

    def _write(self, line: bytes, cancelled: Callable[[], bool] = lambda: False) -> None:
        with self.gate:
            if cancelled():
                raise TransferCancelledError
            payload = line + b"\n"
            if self.port.write(payload) != len(payload):
                raise DisplayProtocolError("Incomplete display write")

    def _read(self) -> bytes | None:
        self.partial.extend(self.port.read_until(b"\n", 4096))
        if len(self.partial) > 4096:
            self.partial.clear()
            raise DisplayProtocolError("Display response too long")
        if self.partial.endswith(b"\n"):
            line = bytes(self.partial).strip()
            self.partial.clear()
            return line
        return None

    def _response(self) -> bytes:
        deadline = self.clock() + 1
        while self.clock() < deadline:
            line = self._read()
            if line and not line.startswith(b"EVT ") and line != b"PONG":
                return line
        raise DisplayProtocolError("Display response timed out")

    def _expect(self, expected: bytes, *, cancelled: Callable[[], bool] = lambda: False) -> None:
        deadline = self.clock() + 1
        while self.clock() < deadline:
            if cancelled():
                raise TransferCancelledError
            line = self._read()
            if line is None:
                continue
            if line == expected:
                return
            if line.startswith(b"ERR ") or (expected.startswith(b"ACK ") and line.startswith(b"ACK")):
                raise DisplayProtocolError("Display rejected frame or acknowledged the wrong byte count")
        raise DisplayProtocolError("Display response timed out")

    def _synchronize(self) -> None:
        self._write(b"")
        deadline = self.clock() + 0.2
        while self.clock() < deadline:
            self._read()
        self.partial.clear()

    def command(self, command: bytes, *, cancelled: Callable[[], bool] = lambda: False) -> None:
        self._write(command, cancelled)
        self._expect(b"OK " + command.removeprefix(b"EXPR "), cancelled=cancelled)

    def initialize(self) -> str:
        self._synchronize()
        self._write(b"STAT")
        self.original_power = parse_power(self._response())
        for attempt in range(2):
            if attempt:
                self._synchronize()
            self._write(b"FRAME BEGIN 168 168 GRAY8")
            try:
                response = self._response()
            except DisplayProtocolError:
                response = b""
            if response == b"OK FRAME BEGIN":
                self.mode = "frame"
                self.send_frame(bytes(168 * 168), 168, 168, "GRAY8", begun=True)
                return self.mode
            if response == b"ERR FRAME (try LIST)":
                break
        if not self.original_power:
            # An unacknowledged POWER ON may still have changed the device.
            self.powered_on = True
            self.command(b"POWER ON")
        return self.mode

    def send_frame(
        self,
        pixels: bytes,
        width: int,
        height: int,
        format_: str,
        *,
        cancelled: Callable[[], bool] = lambda: False,
        begun: bool = False,
    ) -> bool:
        if format_ not in {"GRAY8", "RGB565"} or not 1 <= width <= 169 or not 1 <= height <= 169:
            raise ValueError("Invalid display frame format or dimensions")
        if len(pixels) != width * height * (1 if format_ == "GRAY8" else 2):
            raise ValueError("Invalid display frame size")
        for attempt in range(2):
            try:
                if not begun or attempt:
                    self._write(f"FRAME BEGIN {width} {height} {format_}".encode(), cancelled)
                    self._expect(b"OK FRAME BEGIN", cancelled=cancelled)
                for offset in range(0, len(pixels), 1536):
                    chunk = pixels[offset : offset + 1536]
                    self._write(b"FRAME DATA " + base64.b64encode(chunk), cancelled)
                    self._expect(f"ACK {offset + len(chunk)}".encode(), cancelled=cancelled)
                self._write(b"FRAME END", cancelled)
                self._expect(b"OK FRAME", cancelled=cancelled)
                return True
            except (DisplayProtocolError, TransferCancelledError) as error:
                self._write(b"FRAME ABORT")
                self._expect(b"OK FRAME ABORT")
                if isinstance(error, TransferCancelledError):
                    return False
                if attempt:
                    raise DisplayProtocolError("Display frame transfer failed") from None
        return False

    def ping(self) -> None:
        self._write(b"PING")
        self._expect(b"PONG")

    def restore(self) -> None:
        if self.original_power is None:
            return
        try:
            self.command(b"IDLE" if self.original_power else b"EXPR SLEEPING")
        finally:
            if self.powered_on:
                self.command(b"POWER OFF")


ASCII_FONT: dict[str, tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "!": (0, 0, 16, 16, 16, 16, 16, 16, 0, 16, 0, 0),
    '"': (0, 0, 20, 20, 20, 0, 0, 0, 0, 0, 0, 0),
    "#": (0, 0, 40, 40, 60, 40, 124, 40, 40, 0, 0, 0),
    "$": (0, 16, 16, 40, 32, 48, 8, 4, 4, 56, 16, 0),
    "%": (0, 0, 196, 40, 40, 54, 217, 41, 41, 70, 0, 0),
    "&": (0, 0, 24, 40, 40, 16, 50, 74, 68, 59, 0, 0),
    "'": (0, 0, 8, 8, 8, 0, 0, 0, 0, 0, 0, 0),
    "(": (0, 8, 16, 16, 16, 16, 16, 16, 16, 16, 8, 0),
    ")": (0, 32, 16, 16, 16, 16, 16, 16, 16, 16, 32, 0),
    "*": (0, 0, 8, 28, 24, 0, 0, 0, 0, 0, 0, 0),
    "+": (0, 0, 0, 16, 16, 124, 16, 16, 0, 0, 0, 0),
    ",": (0, 0, 0, 0, 0, 0, 0, 0, 8, 8, 0, 0),
    "-": (0, 0, 0, 0, 0, 0, 0, 56, 0, 0, 0, 0),
    ".": (0, 0, 0, 0, 0, 0, 0, 0, 8, 0, 0, 0),
    "/": (0, 0, 4, 8, 8, 8, 16, 16, 16, 32, 32, 32),
    "0": (0, 0, 56, 36, 68, 68, 68, 68, 36, 56, 0, 0),
    "1": (0, 0, 48, 16, 16, 16, 16, 16, 16, 60, 0, 0),
    "2": (0, 0, 56, 4, 4, 8, 8, 16, 48, 124, 0, 0),
    "3": (0, 0, 56, 4, 4, 24, 4, 4, 4, 56, 0, 0),
    "4": (0, 0, 8, 24, 24, 40, 72, 124, 8, 8, 0, 0),
    "5": (0, 0, 60, 32, 32, 56, 4, 4, 4, 56, 0, 0),
    "6": (0, 0, 28, 32, 32, 56, 36, 36, 36, 24, 0, 0),
    "7": (0, 0, 124, 8, 8, 8, 16, 16, 16, 16, 0, 0),
    "8": (0, 0, 56, 36, 36, 56, 44, 68, 68, 56, 0, 0),
    "9": (0, 0, 56, 68, 68, 68, 60, 4, 8, 56, 0, 0),
    ":": (0, 0, 0, 0, 8, 0, 0, 0, 8, 0, 0, 0),
    ";": (0, 0, 0, 0, 8, 0, 0, 0, 8, 8, 0, 0),
    "<": (0, 0, 0, 0, 4, 56, 96, 28, 0, 0, 0, 0),
    "=": (0, 0, 0, 0, 0, 124, 0, 124, 0, 0, 0, 0),
    ">": (0, 0, 0, 0, 96, 24, 4, 56, 0, 0, 0, 0),
    "?": (0, 0, 24, 4, 4, 4, 8, 8, 0, 8, 0, 0),
    "@": (0, 0, 0, 60, 66, 149, 169, 169, 170, 148, 192, 56),
    "A": (0, 0, 24, 24, 24, 36, 36, 60, 66, 66, 0, 0),
    "B": (0, 0, 60, 34, 34, 60, 34, 34, 34, 60, 0, 0),
    "C": (0, 0, 28, 32, 32, 32, 32, 32, 34, 28, 0, 0),
    "D": (0, 0, 60, 34, 34, 34, 34, 34, 34, 60, 0, 0),
    "E": (0, 0, 60, 32, 32, 60, 32, 32, 32, 60, 0, 0),
    "F": (0, 0, 60, 32, 32, 60, 32, 32, 32, 32, 0, 0),
    "G": (0, 0, 30, 34, 32, 32, 38, 34, 34, 30, 0, 0),
    "H": (0, 0, 68, 68, 68, 124, 68, 68, 68, 68, 0, 0),
    "I": (0, 0, 8, 8, 8, 8, 8, 8, 8, 8, 0, 0),
    "J": (0, 0, 8, 8, 8, 8, 8, 8, 8, 56, 0, 0),
    "K": (0, 0, 34, 36, 40, 56, 52, 36, 34, 34, 0, 0),
    "L": (0, 0, 32, 32, 32, 32, 32, 32, 32, 60, 0, 0),
    "M": (0, 0, 66, 102, 102, 102, 90, 90, 74, 66, 0, 0),
    "N": (0, 0, 68, 100, 100, 84, 84, 76, 76, 68, 0, 0),
    "O": (0, 0, 56, 68, 66, 66, 66, 66, 68, 56, 0, 0),
    "P": (0, 0, 60, 34, 34, 34, 60, 32, 32, 32, 0, 0),
    "Q": (0, 0, 56, 68, 66, 66, 66, 66, 68, 56, 24, 6),
    "R": (0, 0, 60, 34, 34, 34, 60, 36, 36, 34, 0, 0),
    "S": (0, 0, 28, 32, 32, 48, 12, 4, 4, 60, 0, 0),
    "T": (0, 0, 126, 16, 16, 16, 16, 16, 16, 16, 0, 0),
    "U": (0, 0, 68, 68, 68, 68, 68, 68, 68, 56, 0, 0),
    "V": (0, 0, 66, 68, 36, 36, 40, 40, 24, 24, 0, 0),
    "W": (0, 0, 137, 137, 89, 85, 85, 102, 102, 34, 0, 0),
    "X": (0, 0, 36, 36, 24, 24, 24, 40, 36, 68, 0, 0),
    "Y": (0, 0, 136, 72, 80, 80, 32, 32, 32, 32, 0, 0),
    "Z": (0, 0, 62, 4, 8, 8, 16, 16, 32, 126, 0, 0),
    "[": (0, 0, 24, 16, 16, 16, 16, 16, 16, 16, 16, 24),
    "\\": (0, 0, 32, 32, 16, 16, 16, 16, 8, 8, 8, 4),
    "]": (0, 0, 48, 16, 16, 16, 16, 16, 16, 16, 16, 48),
    "^": (0, 0, 16, 24, 40, 40, 0, 0, 0, 0, 0, 0),
    "_": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 124),
    "`": (0, 16, 16, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "a": (0, 0, 0, 0, 56, 4, 12, 52, 36, 60, 0, 0),
    "b": (0, 32, 32, 60, 36, 34, 34, 36, 60, 0, 0, 0),
    "c": (0, 0, 0, 0, 28, 32, 32, 32, 32, 28, 0, 0),
    "d": (0, 4, 4, 28, 36, 36, 36, 36, 60, 0, 0, 0),
    "e": (0, 0, 0, 0, 24, 36, 60, 32, 32, 28, 0, 0),
    "f": (0, 12, 16, 16, 56, 16, 16, 16, 16, 16, 0, 0),
    "g": (0, 0, 0, 0, 60, 36, 36, 56, 32, 60, 68, 68),
    "h": (0, 32, 32, 60, 36, 36, 36, 36, 36, 0, 0, 0),
    "i": (0, 8, 0, 8, 8, 8, 8, 8, 8, 0, 0, 0),
    "j": (0, 16, 0, 16, 16, 16, 16, 16, 16, 16, 16, 32),
    "k": (0, 32, 32, 32, 36, 40, 48, 56, 36, 36, 0, 0),
    "l": (0, 8, 8, 8, 8, 8, 8, 8, 8, 0, 0, 0),
    "m": (0, 0, 0, 0, 246, 154, 146, 146, 146, 146, 0, 0),
    "n": (0, 0, 0, 0, 60, 36, 36, 36, 36, 36, 0, 0),
    "o": (0, 0, 0, 0, 24, 36, 34, 34, 36, 24, 0, 0),
    "p": (0, 0, 0, 0, 60, 36, 34, 34, 36, 60, 32, 32),
    "q": (0, 0, 0, 0, 28, 36, 36, 36, 36, 60, 4, 4),
    "r": (0, 0, 0, 0, 28, 16, 16, 16, 16, 16, 0, 0),
    "s": (0, 0, 0, 0, 28, 32, 16, 12, 4, 28, 0, 0),
    "t": (0, 0, 16, 16, 56, 16, 16, 16, 16, 28, 0, 0),
    "u": (0, 0, 0, 0, 36, 36, 36, 36, 36, 60, 0, 0),
    "v": (0, 0, 0, 0, 68, 36, 40, 40, 40, 16, 0, 0),
    "w": (0, 0, 0, 0, 153, 90, 90, 106, 102, 36, 0, 0),
    "x": (0, 0, 0, 0, 20, 20, 8, 8, 20, 34, 0, 0),
    "y": (0, 0, 0, 0, 68, 36, 40, 40, 40, 16, 16, 16),
    "z": (0, 0, 0, 0, 28, 4, 8, 8, 16, 60, 0, 0),
    "{": (0, 0, 24, 16, 16, 16, 48, 16, 16, 16, 16, 8),
    "|": (0, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8, 8),
    "}": (0, 0, 48, 16, 16, 16, 8, 16, 16, 16, 16, 32),
    "~": (0, 0, 0, 0, 0, 32, 20, 8, 0, 0, 0, 0),
}


def draw_ascii(pixels: bytearray, width: int, height: int, text: str, x: int, y: int, *, color: int = 0xFFFF) -> None:
    ink = color.to_bytes(2, "little")
    for character in text:
        for row, bits in enumerate(ASCII_FONT.get(character, ASCII_FONT.get("?", ()))):
            for column in range(8):
                px, py = x + column, y + row
                if bits & (0x80 >> column) and 0 <= px < width and 0 <= py < height:
                    offset = (py * width + px) * 2
                    pixels[offset : offset + 2] = ink
        x += 8


def state_screen(state: str, fields: dict[str, Any]) -> bytes:
    suffix = f"-{fields['reason']}" if state in {"rejected", "failed"} else ""
    pixels = bytearray(Path(__file__).with_name(f"{state}{suffix}.bin").read_bytes())
    if len(pixels) != 168 * 168 * 2:
        raise ValueError("Invalid state screen asset")
    value = fields.get("ssid", "") if state == "connecting" else fields.get("hostname", "")
    if value and state in {"connecting", "connected"} and all(32 <= ord(character) <= 126 for character in value):
        if state == "connected":
            value += ".local"
        lines = [value[index : index + 18] for index in range(0, min(len(value), 72), 18)]
        for index, line in enumerate(lines):
            draw_ascii(pixels, 168, 168, line, (168 - len(line) * 8) // 2, 64 + index * 12)
    return bytes(pixels)


def open_display_port() -> Any:
    serial = importlib.import_module("serial")
    ports = importlib.import_module("serial.tools.list_ports")
    devices = sorted(port.device for port in ports.comports() if port.vid == 0x2E8A)
    if not devices:
        raise DeviceUnavailableError("Face display unavailable")
    try:
        return serial.Serial(devices[0], 115200, timeout=0.05, write_timeout=0.2)
    except OSError as error:
        if error.errno in {errno.ENOENT, errno.ENODEV, errno.ENXIO}:
            raise DeviceUnavailableError("Face display unavailable") from None
        raise


class Display:
    """Expression fallback: scanning=CURIOUS, checking/connecting=THINKING,
    rejected=BATSU, failed=SAD, connected=HAPPY.
    """

    EXPRESSIONS: ClassVar[dict[str, str]] = {
        "scanning": "CURIOUS",
        "checking": "THINKING",
        "connecting": "THINKING",
        "rejected": "BATSU",
        "failed": "SAD",
        "connected": "HAPPY",
    }

    def __init__(
        self, *, port_factory: Callable[[], Any] = open_display_port, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.condition = threading.Condition(threading.RLock())
        self.ready = threading.Event()
        self.stopping = False
        self.failure: Exception | None = None
        self.generation = 0
        self.state = "scanning"
        self.mode = "expr"
        self.pending_screen: tuple[str, bytes, int] | None = None
        self.pending_live: bytes | None = None
        self.pending_ping = False
        self.thread = threading.Thread(
            target=self._run, args=(port_factory, clock), daemon=True, name="wifi-qr-display"
        )
        self.thread.start()
        self.ready.wait()
        try:
            self.check()
        except BaseException:
            self.close()
            raise

    def check(self) -> None:
        if self.failure is not None:
            raise self.failure from None

    def screen(self, state: str, fields: dict[str, Any]) -> None:
        self.check()
        format_ = "GRAY8" if state == "scanning" else "RGB565"
        pixels = bytes(168 * 168) if state == "scanning" or self.mode == "expr" else state_screen(state, fields)
        with self.condition:
            self.state = state
            self.generation += 1
            self.pending_live = None
            self.pending_screen = (format_, pixels, self.generation)
            self.condition.notify()

    def live(self, frame: bytes | None) -> None:
        self.check()
        if frame is None or self.mode != "frame":
            return
        if len(frame) != 168 * 168:
            raise ValueError("Invalid live preview size")
        with self.condition:
            if self.state == "scanning":
                self.pending_live = frame
                self.condition.notify()

    def ping(self) -> None:
        self.check()
        with self.condition:
            self.pending_ping = True
            self.condition.notify()

    def _run(self, port_factory: Callable[[], Any], clock: Callable[[], float]) -> None:
        port: Any = None
        protocol: FrameClient | None = None
        try:
            port = port_factory()
            protocol = FrameClient(port, clock=clock, gate=self.condition)
            self.mode = protocol.initialize()
            self.ready.set()
            while True:
                request: tuple[str, bytes | None, int, str] | None
                with self.condition:
                    self.condition.wait_for(
                        lambda: (
                            self.stopping
                            or self.pending_screen is not None
                            or self.pending_live is not None
                            or self.pending_ping
                        )
                    )
                    if self.stopping:
                        break
                    if self.pending_screen is not None:
                        request = (*self.pending_screen, self.EXPRESSIONS[self.state])
                        self.pending_screen = None
                    elif self.pending_ping:
                        self.pending_ping = False
                        request = None
                    else:
                        request = ("GRAY8", self.pending_live, self.generation, "CURIOUS")
                        self.pending_live = None
                if request is None:
                    if self.mode == "frame":
                        protocol.ping()
                    continue
                format_, pixels, generation, expression = request

                def cancelled(generation: int = generation) -> bool:
                    return self.stopping or generation != self.generation

                if self.mode == "expr":
                    with suppress(TransferCancelledError):
                        protocol.command(f"EXPR {expression}".encode(), cancelled=cancelled)
                elif pixels is not None:
                    protocol.send_frame(
                        pixels,
                        168,
                        168,
                        format_,
                        cancelled=cancelled,
                    )
        except DeviceUnavailableError:
            self.failure = DeviceUnavailableError("Face display unavailable")
        except Exception:
            self.failure = RuntimeError("Display communication failed")
        finally:
            self.ready.set()
            if protocol is not None:
                try:
                    protocol.restore()
                except Exception:
                    if self.failure is None:
                        self.failure = RuntimeError("Display restore failed")
            if port is not None:
                try:
                    port.close()
                except Exception:
                    if self.failure is None:
                        self.failure = RuntimeError("Display close failed")

    def close(self) -> None:
        with self.condition:
            self.stopping = True
            self.generation += 1
            self.pending_live = None
            self.pending_screen = None
            self.condition.notify()
        self.thread.join()


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
        if hasattr(self.decoder, "close"):
            self.stack.callback(self._cleanup, self.decoder.close)
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
            self.display.live(getattr(self.decoder, "preview", frame))

    def step(self) -> bool:
        if self.stopped:
            return False
        self._accept()
        try:
            if hasattr(self.display, "check"):
                self.display.check()
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
