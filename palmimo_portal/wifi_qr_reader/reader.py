"""Isolated hardware owner; no Portal imports. Protocol: see doc/wifi-qr-reader.md."""

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


class ReaderOperationError(RuntimeError):
    def __init__(self, message: str, *, error: BaseException | None = None) -> None:
        super().__init__(message)
        self.error_type: str = (
            (error.error_type if isinstance(error, ReaderOperationError) else type(error).__name__)
            if error is not None
            else type(self).__name__
        )
        number = getattr(error, "errno", None)
        self.errno = number if isinstance(number, int) and not isinstance(number, bool) else None


class DeviceUnavailableError(ReaderOperationError):
    pass


class UnsupportedFirmwareError(ReaderOperationError):
    pass


class DependencyUnavailableError(ReaderOperationError):
    pass


class FrameDecodeError(ValueError):
    pass


def valid_holder(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    holder: dict[str, Any] = {}
    pid = metadata.get("pid")
    if isinstance(pid, int) and not isinstance(pid, bool) and 0 < pid < 2**31:
        holder["pid"] = pid
    for key in ("user", "app"):
        value = metadata.get(key)
        if isinstance(value, str) and 0 < len(value) <= 128 and value.isprintable():
            holder[key] = value
    return holder


class ReservationUnavailableError(ReaderOperationError):
    def __init__(
        self, resource: str, holder: dict[str, Any] | None = None, *, error: BaseException | None = None
    ) -> None:
        super().__init__("Hardware reservation unavailable", error=error)
        self.resource = resource
        self.holder = valid_holder(holder)


class ReservationSetupError(DeviceUnavailableError):
    def __init__(self, resource: str, path: Path, error: OSError) -> None:
        super().__init__("Reservation storage unavailable", error=error)
        self.resource = resource
        self.path = path


def log_failure(message: str, error: BaseException, *, level: int = logging.WARNING, **context: Any) -> None:
    kind = error.error_type if isinstance(error, ReaderOperationError) else type(error).__name__
    number = getattr(error, "errno", None)
    if not isinstance(number, int) or isinstance(number, bool):
        number = None
    LOG.log(
        level,
        "%s type=%s errno=%s context=%s",
        message,
        kind,
        number,
        context,
        extra={"error_type": kind, "error_errno": number, "error_context": context},
    )


def camera_failure(error: OSError) -> BaseException:
    if error.errno == errno.EBUSY:
        return ReservationUnavailableError("camera", error=error)
    if error.errno in {errno.ENOENT, errno.ENODEV, errno.ENXIO, errno.EIO}:
        return DeviceUnavailableError("Camera unavailable", error=error)
    return error


def display_failure(error: BaseException) -> ReaderOperationError:
    if isinstance(error, ReaderOperationError):
        return error
    if isinstance(error, OSError):
        if error.errno == errno.EBUSY:
            return ReservationUnavailableError("display", error=error)
        return DeviceUnavailableError("Face display unavailable", error=error)
    return ReaderOperationError("Display communication failed", error=error)


@contextmanager
def reserve_resource(resource: str, directory: Path = Path("/run/lock/palmimo")) -> Iterator[None]:
    """Share the SDK's flock inode, permissions and holder metadata; never unlink it."""
    fd: int | None = None
    path = directory / f"{resource}.lock"
    try:
        if not stat.S_ISDIR(directory.lstat().st_mode):
            raise OSError(errno.EINVAL, "Unsafe reservation directory")
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
            raise OSError(errno.EINVAL, "Unsafe reservation file")
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            if error.errno not in {errno.EAGAIN, errno.EWOULDBLOCK}:
                raise
            holder: dict[str, Any] = {}
            with suppress(OSError, ValueError, RecursionError):
                holder = valid_holder(json.loads(os.pread(fd, 65536, 0)))
            raise ReservationUnavailableError(resource, holder, error=error) from None
        uid = os.geteuid()
        try:
            user = pwd.getpwuid(uid).pw_name
        except KeyError:
            user = str(uid)
        metadata = {
            "pid": os.getpid(),
            "user": user,
            "app": "palmimo-wifi-qr",
            "acquired_at": datetime.now(UTC).isoformat(),
        }
        with os.fdopen(os.dup(fd), "w", encoding="utf-8") as stream:
            stream.seek(0)
            json.dump(metadata, stream)
            stream.truncate()
    except OSError as error:
        if fd is not None:
            os.close(fd)
        raise ReservationSetupError(resource, path, error) from None
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
            raise DependencyUnavailableError("Required hardware dependency unavailable")
        ctypes.CDLL(library)
    except (ImportError, OSError) as error:
        raise DependencyUnavailableError("Required hardware dependency unavailable", error=error) from None


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


LIBC = ctypes.CDLL(None, use_errno=True)
LIBC.ioctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_void_p]
LIBC.ioctl.restype = ctypes.c_int


def video_ioctl(fd: int, request: int, argument: Any) -> None:
    while LIBC.ioctl(fd, request, ctypes.byref(argument)) < 0:
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
            if isinstance(error, OSError):
                raise camera_failure(error) from None
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
                    continue
                if buffer.bytesused and not buffer.flags & 0x40:
                    return bytes(self.buffers[buffer.index][: buffer.bytesused])
            if dequeued:
                raise FrameDecodeError("Invalid camera buffer")
            return None
        except OSError as error:
            raise camera_failure(error) from None
        finally:
            for buffer in dequeued:
                if buffer.index < len(self.buffers):
                    try:
                        self.ioctl(self.fd, VIDIOC_QBUF, buffer)
                    except OSError as error:
                        raise camera_failure(error) from None

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
    edges = [x * side // size for x in range(size + 1)]
    output = bytearray()
    for y in range(size):
        y0, y1 = edges[y], max(edges[y] + 1, edges[y + 1])
        rows = [pixels[(top + row) * width + left : (top + row) * width + left + side] for row in range(y0, y1)]
        columns = [sum(values) for values in zip(*rows, strict=True)]
        for x in range(size):
            x0, x1 = edges[x], max(edges[x] + 1, edges[x + 1])
            output.append(sum(columns[x0:x1]) // ((x1 - x0) * (y1 - y0)))
    # The camera is mounted upside down; QR decoding itself is orientation independent.
    return bytes(output)[::-1]


TJERR_WARNING = 0


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
        self.library.tjGetErrorCode.argtypes = [ctypes.c_void_p]
        self.library.tjGetErrorCode.restype = ctypes.c_int
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
            and self.library.tjGetErrorCode(self.handle) != TJERR_WARNING
        ):
            raise FrameDecodeError("Invalid camera JPEG")
        if not 0 < width.value <= 2592 or not 0 < height.value <= 1944 or denominator not in {2, 4}:
            raise FrameDecodeError("Invalid camera JPEG dimensions or scale")
        w, h = (width.value + denominator - 1) // denominator, (height.value + denominator - 1) // denominator
        output = ctypes.create_string_buffer(w * h)
        # libjpeg-turbo completes the output on warnings unless STOPONWARNING is set.
        if (
            self.library.tjDecompress2(self.handle, jpeg, len(jpeg), output, w, 0, h, 6, 0) != 0
            and self.library.tjGetErrorCode(self.handle) != TJERR_WARNING
        ):
            raise FrameDecodeError("Camera JPEG decode failed")
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
    def __init__(
        self,
        port: Any,
        *,
        clock: Callable[[], float] = time.monotonic,
        gate: Any = None,
        on_tap: Callable[[int, int], None] = lambda x, y: None,
    ) -> None:
        self.port = port
        self.clock = clock
        self.gate = gate if gate is not None else threading.RLock()
        self.partial = bytearray()
        self.original_power: bool | None = None
        self.firmware = "unknown"
        self.on_tap = on_tap
        self.transferring = False
        self.frame_active = False

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
            if not self.transferring and line.startswith(b"EVT TAP "):
                fields = line.split()
                if len(fields) == 4:
                    try:
                        x, y = int(fields[2]), int(fields[3])
                    except ValueError:
                        pass
                    else:
                        self.on_tap(x, y)
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

    def initialize(self) -> None:
        self._synchronize()
        try:
            self._write(b"VERSION")
            try:
                version = self._response()
            except DisplayProtocolError:
                version = b""
            safe_version = 0 < len(version) <= 256 and all(32 <= byte <= 126 for byte in version)
            if safe_version and version.startswith(b"VERSION "):
                self.firmware = version[8:].decode("ascii")
            LOG.info(
                "%s",
                version.decode("ascii") if safe_version else "VERSION unavailable",
                extra={"display_firmware": self.firmware},
            )
            for attempt in range(2):
                if attempt:
                    self._synchronize()
                self._write(b"FRAME BEGIN 168 168 GRAY8")
                try:
                    response = self._response()
                except DisplayProtocolError:
                    response = b""
                if response == b"ERR FRAME (try LIST)":
                    raise UnsupportedFirmwareError("Display firmware does not support frames")
                if response == b"OK FRAME BEGIN":
                    self.frame_active = True
                    break
            else:
                raise DeviceUnavailableError("Face display did not acknowledge frames")
            self._write(b"STAT")
            self.original_power = parse_power(self._response())
            self.send_frame(bytes(168 * 168), 168, 168, "GRAY8", begun=True)
        except DisplayProtocolError as error:
            raise DeviceUnavailableError("Face display did not initialize", error=error) from None

    def poll(self) -> None:
        if getattr(self.port, "in_waiting", 1):
            self._read()

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
        self.transferring = True
        try:
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
        finally:
            self.transferring = False

    def ping(self) -> None:
        self._write(b"PING")
        self._expect(b"PONG")

    def restore(self) -> None:
        if self.frame_active and self.original_power is not None:
            self.command(b"IDLE" if self.original_power else b"EXPR SLEEPING")


FONT_WIDTH = 6
FONT_HEIGHT = 10
QR_RECT = (39, 40, 129, 130)
CLOSE_RECT = (50, 132, 118, 149)


# X.Org font-misc-misc 1.1.3, 6x10.bdf (public domain): https://www.x.org/releases/individual/font/font-misc-misc-1.1.3.tar.xz
ASCII_FONT: dict[str, tuple[int, ...]] = {
    " ": (0, 0, 0, 0, 0, 0, 0, 0, 0, 0),
    "!": (0, 32, 32, 32, 32, 32, 0, 32, 0, 0),
    '"': (0, 80, 80, 80, 0, 0, 0, 0, 0, 0),
    "#": (0, 80, 80, 248, 80, 248, 80, 80, 0, 0),
    "$": (0, 32, 112, 160, 112, 40, 112, 32, 0, 0),
    "%": (0, 72, 168, 80, 32, 80, 168, 144, 0, 0),
    "&": (0, 64, 160, 160, 64, 168, 144, 104, 0, 0),
    "'": (0, 32, 32, 32, 0, 0, 0, 0, 0, 0),
    "(": (0, 16, 32, 64, 64, 64, 32, 16, 0, 0),
    ")": (0, 64, 32, 16, 16, 16, 32, 64, 0, 0),
    "*": (0, 0, 136, 80, 248, 80, 136, 0, 0, 0),
    "+": (0, 0, 32, 32, 248, 32, 32, 0, 0, 0),
    ",": (0, 0, 0, 0, 0, 0, 48, 32, 64, 0),
    "-": (0, 0, 0, 0, 248, 0, 0, 0, 0, 0),
    ".": (0, 0, 0, 0, 0, 0, 32, 112, 32, 0),
    "/": (0, 8, 8, 16, 32, 64, 128, 128, 0, 0),
    "0": (0, 32, 80, 136, 136, 136, 80, 32, 0, 0),
    "1": (0, 32, 96, 160, 32, 32, 32, 248, 0, 0),
    "2": (0, 112, 136, 8, 48, 64, 128, 248, 0, 0),
    "3": (0, 248, 8, 16, 48, 8, 136, 112, 0, 0),
    "4": (0, 16, 48, 80, 144, 248, 16, 16, 0, 0),
    "5": (0, 248, 128, 176, 200, 8, 136, 112, 0, 0),
    "6": (0, 48, 64, 128, 176, 200, 136, 112, 0, 0),
    "7": (0, 248, 8, 16, 16, 32, 64, 64, 0, 0),
    "8": (0, 112, 136, 136, 112, 136, 136, 112, 0, 0),
    "9": (0, 112, 136, 152, 104, 8, 16, 96, 0, 0),
    ":": (0, 0, 32, 112, 32, 0, 32, 112, 32, 0),
    ";": (0, 0, 32, 112, 32, 0, 48, 32, 64, 0),
    "<": (0, 8, 16, 32, 64, 32, 16, 8, 0, 0),
    "=": (0, 0, 0, 248, 0, 248, 0, 0, 0, 0),
    ">": (0, 64, 32, 16, 8, 16, 32, 64, 0, 0),
    "?": (0, 112, 136, 16, 32, 32, 0, 32, 0, 0),
    "@": (0, 112, 136, 152, 168, 176, 128, 112, 0, 0),
    "A": (0, 32, 80, 136, 136, 248, 136, 136, 0, 0),
    "B": (0, 240, 72, 72, 112, 72, 72, 240, 0, 0),
    "C": (0, 112, 136, 128, 128, 128, 136, 112, 0, 0),
    "D": (0, 240, 72, 72, 72, 72, 72, 240, 0, 0),
    "E": (0, 248, 128, 128, 240, 128, 128, 248, 0, 0),
    "F": (0, 248, 128, 128, 240, 128, 128, 128, 0, 0),
    "G": (0, 112, 136, 128, 128, 152, 136, 112, 0, 0),
    "H": (0, 136, 136, 136, 248, 136, 136, 136, 0, 0),
    "I": (0, 112, 32, 32, 32, 32, 32, 112, 0, 0),
    "J": (0, 56, 16, 16, 16, 16, 144, 96, 0, 0),
    "K": (0, 136, 144, 160, 192, 160, 144, 136, 0, 0),
    "L": (0, 128, 128, 128, 128, 128, 128, 248, 0, 0),
    "M": (0, 136, 136, 216, 168, 136, 136, 136, 0, 0),
    "N": (0, 136, 136, 200, 168, 152, 136, 136, 0, 0),
    "O": (0, 112, 136, 136, 136, 136, 136, 112, 0, 0),
    "P": (0, 240, 136, 136, 240, 128, 128, 128, 0, 0),
    "Q": (0, 112, 136, 136, 136, 136, 168, 112, 8, 0),
    "R": (0, 240, 136, 136, 240, 160, 144, 136, 0, 0),
    "S": (0, 112, 136, 128, 112, 8, 136, 112, 0, 0),
    "T": (0, 248, 32, 32, 32, 32, 32, 32, 0, 0),
    "U": (0, 136, 136, 136, 136, 136, 136, 112, 0, 0),
    "V": (0, 136, 136, 136, 80, 80, 80, 32, 0, 0),
    "W": (0, 136, 136, 136, 168, 168, 216, 136, 0, 0),
    "X": (0, 136, 136, 80, 32, 80, 136, 136, 0, 0),
    "Y": (0, 136, 136, 80, 32, 32, 32, 32, 0, 0),
    "Z": (0, 248, 8, 16, 32, 64, 128, 248, 0, 0),
    "[": (0, 112, 64, 64, 64, 64, 64, 112, 0, 0),
    "\\": (0, 128, 128, 64, 32, 16, 8, 8, 0, 0),
    "]": (0, 112, 16, 16, 16, 16, 16, 112, 0, 0),
    "^": (0, 32, 80, 136, 0, 0, 0, 0, 0, 0),
    "_": (0, 0, 0, 0, 0, 0, 0, 0, 248, 0),
    "`": (32, 16, 0, 0, 0, 0, 0, 0, 0, 0),
    "a": (0, 0, 0, 112, 8, 120, 136, 120, 0, 0),
    "b": (0, 128, 128, 176, 200, 136, 200, 176, 0, 0),
    "c": (0, 0, 0, 112, 136, 128, 136, 112, 0, 0),
    "d": (0, 8, 8, 104, 152, 136, 152, 104, 0, 0),
    "e": (0, 0, 0, 112, 136, 248, 128, 112, 0, 0),
    "f": (0, 48, 72, 64, 240, 64, 64, 64, 0, 0),
    "g": (0, 0, 0, 120, 136, 136, 120, 8, 136, 112),
    "h": (0, 128, 128, 176, 200, 136, 136, 136, 0, 0),
    "i": (0, 32, 0, 96, 32, 32, 32, 112, 0, 0),
    "j": (0, 8, 0, 24, 8, 8, 8, 72, 72, 48),
    "k": (0, 128, 128, 136, 144, 224, 144, 136, 0, 0),
    "l": (0, 96, 32, 32, 32, 32, 32, 112, 0, 0),
    "m": (0, 0, 0, 208, 168, 168, 168, 136, 0, 0),
    "n": (0, 0, 0, 176, 200, 136, 136, 136, 0, 0),
    "o": (0, 0, 0, 112, 136, 136, 136, 112, 0, 0),
    "p": (0, 0, 0, 176, 200, 136, 200, 176, 128, 128),
    "q": (0, 0, 0, 104, 152, 136, 152, 104, 8, 8),
    "r": (0, 0, 0, 176, 200, 128, 128, 128, 0, 0),
    "s": (0, 0, 0, 112, 128, 112, 8, 240, 0, 0),
    "t": (0, 64, 64, 240, 64, 64, 72, 48, 0, 0),
    "u": (0, 0, 0, 136, 136, 136, 152, 104, 0, 0),
    "v": (0, 0, 0, 136, 136, 80, 80, 32, 0, 0),
    "w": (0, 0, 0, 136, 136, 168, 168, 80, 0, 0),
    "x": (0, 0, 0, 136, 80, 32, 80, 136, 0, 0),
    "y": (0, 0, 0, 136, 136, 152, 104, 8, 136, 112),
    "z": (0, 0, 0, 248, 16, 32, 64, 248, 0, 0),
    "{": (0, 24, 32, 16, 96, 16, 32, 24, 0, 0),
    "|": (0, 32, 32, 32, 32, 32, 32, 32, 0, 0),
    "}": (0, 96, 16, 32, 24, 32, 16, 96, 0, 0),
    "~": (0, 72, 168, 144, 0, 0, 0, 0, 0, 0),
    "…": (0, 0, 0, 0, 0, 0, 0, 168, 0, 0),
}


def draw_ascii(
    pixels: bytearray,
    width: int,
    height: int,
    text: str,
    x: int,
    y: int,
    *,
    color: int = 0xFFFF,
    bytes_per_pixel: int = 2,
) -> None:
    ink = color.to_bytes(bytes_per_pixel, "little")
    for character in text:
        for row, bits in enumerate(ASCII_FONT.get(character, ASCII_FONT.get("?", ()))):
            for column in range(FONT_WIDTH):
                px, py = x + column, y + row
                if bits & (0x80 >> column) and 0 <= px < width and 0 <= py < height:
                    offset = (py * width + px) * bytes_per_pixel
                    pixels[offset : offset + bytes_per_pixel] = ink
        x += FONT_WIDTH


def valid_qr(qr: Any) -> bool:
    if not isinstance(qr, dict):
        return False
    size, rows = qr.get("size"), qr.get("rows")
    return (
        isinstance(size, int)
        and not isinstance(size, bool)
        and 1 <= size <= min(QR_RECT[2] - QR_RECT[0], QR_RECT[3] - QR_RECT[1]) - 8
        and isinstance(rows, list)
        and len(rows) == size
        and all(isinstance(row, str) and len(row) == size and set(row) <= {"0", "1"} for row in rows)
    )


def fill_rect(pixels: bytearray, rect: tuple[int, int, int, int], color: int) -> None:
    x0, y0, x1, y1 = rect
    row = color.to_bytes(2, "little") * (x1 - x0)
    for y in range(y0, y1):
        offset = (y * 168 + x0) * 2
        pixels[offset : offset + len(row)] = row


def state_screen(state: str, fields: dict[str, Any]) -> bytes:
    suffix = f"-{fields['reason']}" if state in {"rejected", "failed"} else ""
    pixels = bytearray(Path(__file__).with_name(f"{state}{suffix}.bin").read_bytes())
    if len(pixels) != 168 * 168 * 2:
        raise ValueError("Invalid state screen asset")
    if state == "connected":
        url = fields.get("url", "")
        capacity = 168 // FONT_WIDTH
        text = url if len(url) <= capacity else url[: capacity - 1] + "…"
        draw_ascii(pixels, 168, 168, text, (168 - len(text) * FONT_WIDTH) // 2, 26)
        qr = fields.get("qr")
        if qr is not None:
            if not valid_qr(qr):
                raise ValueError("Invalid connection QR matrix")
            size = qr["size"]
            scale = min(QR_RECT[2] - QR_RECT[0], QR_RECT[3] - QR_RECT[1]) // (size + 8)
            side = (size + 8) * scale
            left = QR_RECT[0] + (QR_RECT[2] - QR_RECT[0] - side) // 2 + 4 * scale
            top = QR_RECT[1] + (QR_RECT[3] - QR_RECT[1] - side) // 2 + 4 * scale
            fill_rect(pixels, QR_RECT, 0xFFFF)
            for y, row in enumerate(qr["rows"]):
                for x, value in enumerate(row):
                    if value == "1":
                        fill_rect(
                            pixels,
                            (left + x * scale, top + y * scale, left + (x + 1) * scale, top + (y + 1) * scale),
                            0,
                        )
    elif state == "connecting":
        ssid = fields.get("ssid", "")
        if ssid and all(32 <= ord(character) <= 126 for character in ssid):
            for index in range(0, min(len(ssid), 56), 28):
                text = ssid[index : index + 28]
                draw_ascii(pixels, 168, 168, text, (168 - len(text) * FONT_WIDTH) // 2, 74 + (index // 28) * 12)
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
        raise display_failure(error) from None
    except Exception as error:
        raise DeviceUnavailableError("Face display unavailable", error=error) from None


class Display:
    def __init__(
        self, *, port_factory: Callable[[], Any] = open_display_port, clock: Callable[[], float] = time.monotonic
    ) -> None:
        self.condition = threading.Condition(threading.RLock())
        self.ready = threading.Event()
        self.stopping = False
        self.failure: Exception | None = None
        self.generation = 0
        self.state = "scanning"
        self.rendered_connected: int | None = None
        self.close_pending = False
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
        pixels = bytes(168 * 168) if state == "scanning" else state_screen(state, fields)
        with self.condition:
            self.state = state
            self.generation += 1
            self.rendered_connected = None
            self.close_pending = False
            self.pending_live = None
            self.pending_screen = (format_, pixels, self.generation)
            self.condition.notify()

    def live(self, frame: bytes | None) -> None:
        self.check()
        if frame is None:
            return
        if len(frame) != 168 * 168:
            raise ValueError("Invalid live preview size")
        pixels = bytearray(frame)
        pixels[154 * 168 :] = bytes(14 * 168)
        text = "Show your Wi-Fi QR"
        draw_ascii(pixels, 168, 168, text, (168 - len(text) * FONT_WIDTH) // 2, 156, color=255, bytes_per_pixel=1)
        with self.condition:
            if self.state == "scanning":
                self.pending_live = bytes(pixels)
                self.condition.notify()

    def ping(self) -> None:
        self.check()
        with self.condition:
            self.pending_ping = True
            self.condition.notify()

    def _tap(self, x: int, y: int) -> None:
        with self.condition:
            image_x, image_y = (x - 72) // 2, (y - 72) // 2
            if (
                not self.stopping
                and self.state == "connected"
                and self.rendered_connected == self.generation
                and CLOSE_RECT[0] <= image_x < CLOSE_RECT[2]
                and CLOSE_RECT[1] <= image_y < CLOSE_RECT[3]
            ):
                self.close_pending = True

    def take_close(self) -> bool:
        with self.condition:
            close = self.close_pending
            self.close_pending = False
            return close

    def _run(self, port_factory: Callable[[], Any], clock: Callable[[], float]) -> None:
        port: Any = None
        protocol: FrameClient | None = None
        try:
            port = port_factory()
            protocol = FrameClient(port, clock=clock, gate=self.condition, on_tap=self._tap)
            protocol.initialize()
            self.ready.set()
            while True:
                request: tuple[str, bytes, int, str] | None
                heartbeat = False
                with self.condition:
                    self.condition.wait_for(
                        lambda: (
                            self.stopping
                            or self.pending_screen is not None
                            or self.pending_live is not None
                            or self.pending_ping
                        ),
                        timeout=0.02,
                    )
                    if self.stopping:
                        break
                    if self.pending_screen is not None:
                        request = (*self.pending_screen, self.state)
                        self.pending_screen = None
                    elif self.pending_ping:
                        self.pending_ping = False
                        heartbeat = True
                        request = None
                    elif self.pending_live is not None:
                        request = ("GRAY8", self.pending_live, self.generation, "scanning")
                        self.pending_live = None
                    else:
                        request = None
                if request is None:
                    if heartbeat:
                        protocol.ping()
                    else:
                        protocol.poll()
                    continue
                format_, pixels, generation, state = request

                def cancelled(generation: int = generation) -> bool:
                    return self.stopping or generation != self.generation

                completed = protocol.send_frame(pixels, 168, 168, format_, cancelled=cancelled)
                with self.condition:
                    if completed and state == "connected" and generation == self.generation and not self.stopping:
                        self.rendered_connected = generation
        except Exception as error:
            self.failure = display_failure(error)
        finally:
            with self.condition:
                self.rendered_connected = None
                self.close_pending = False
            self.ready.set()
            if protocol is not None:
                try:
                    protocol.restore()
                except Exception as error:
                    log_failure("Display restore failed", error)
                    if self.failure is None:
                        self.failure = display_failure(error)
            if port is not None:
                try:
                    port.close()
                except Exception as error:
                    log_failure("Display close failed", error)
                    if self.failure is None:
                        self.failure = display_failure(error)

    def close(self) -> None:
        with self.condition:
            self.stopping = True
            self.generation += 1
            self.rendered_connected = None
            self.close_pending = False
            self.pending_live = None
            self.pending_screen = None
            self.condition.notify()
        self.thread.join()


def encode_message(message: dict[str, Any]) -> bytes:
    return json.dumps(message, ensure_ascii=True, separators=(",", ":")).encode("utf-8")


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
        self.sent_texts: dict[str, int] = {}
        self.frame_failures = 0
        self.incoming = bytearray()
        self.outgoing = bytearray()
        self.discarding_line = False
        self.stopped = False
        self.exit_code = 0
        self.started = False
        self.close_sent = False

    def _cleanup(self, callback: Callable[[], Any]) -> None:
        try:
            callback()
        except Exception as error:
            log_failure("Hardware cleanup failed", error)

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
        except (ImportError, OSError) as error:
            raise DependencyUnavailableError("Required hardware dependency unavailable", error=error) from None
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
        line = encode_message(message)
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
            connection.setblocking(False)
            self._send({"type": "hello", "proto": 1})

    def _line(self, line: bytes) -> None:
        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, RecursionError) as error:
            log_failure("Invalid control message discarded", error)
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
        elif state == "connected":
            url, qr = message.get("url"), message.get("qr")
            if (
                not isinstance(url, str)
                or not url
                or not all(32 <= ord(character) <= 126 for character in url)
                or not valid_qr(qr)
            ):
                return
            fields.update(url=url, qr=qr)
        elif state == "connecting":
            key = "ssid"
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
        try:
            frame = self.camera.read()
            if frame is None:
                return
            texts = self.decoder.decode(frame)
        except ValueError as error:
            self.frame_failures += 1
            if self.frame_failures in {1, 100}:
                log_failure("Frame discarded", error, consecutive_frames=self.frame_failures)
            if self.frame_failures >= 100:
                raise ReaderOperationError("Too many consecutive invalid frames", error=error) from None
            return
        if self.frame_failures:
            LOG.info("Frame decoding recovered count=%d", self.frame_failures)
            self.frame_failures = 0
        texts = [text for text in texts if len(encode_message({"type": "qr", "text": text})) <= MAX_LINE]
        for previous_text in list(self.sent_texts):
            absent_frames = 0 if previous_text in texts else self.sent_texts[previous_text] + 1
            if absent_frames >= 30:
                del self.sent_texts[previous_text]
            else:
                self.sent_texts[previous_text] = absent_frames
        text = next((text for text in texts if text not in self.sent_texts), None)
        if text is not None:
            self._screen("checking", {})
            if self.client is not None:
                if len(self.sent_texts) >= 16:
                    del self.sent_texts[next(iter(self.sent_texts))]
                self.sent_texts[text] = 0
                self._send({"type": "qr", "text": text})
        else:
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
            if now - self.last_ping >= 2:
                self.display.ping()
                self.last_ping = now
            if (
                self.state == "connected"
                and self.client is not None
                and not self.close_sent
                and getattr(self.display, "take_close", lambda: False)()
            ):
                self._send({"type": "close"})
                self.close_sent = True
            if self.state == "scanning":
                self._frame()
            self._flush()
        except (BrokenPipeError, ConnectionResetError):
            self.stop()
        return not self.stopped

    def stop(self) -> None:
        self.stopped = True

    def close(self) -> None:
        try:
            self.stack.close()
        finally:
            if self.client is not None:
                self.client.close()
                self.client = None
        self.incoming.clear()
        self.outgoing.clear()
        self.sent_texts.clear()

    def run(self) -> int:
        try:
            if not self.stopped:
                self.start()
            while self.step():
                self.sleep(0.01)
        except ReservationUnavailableError as error:
            log_failure("Hardware busy", error, resource=error.resource, holder=error.holder)
            self.exit_code = 75
        except UnsupportedFirmwareError as error:
            log_failure("Display firmware unsupported", error)
            self.exit_code = 76
        except DependencyUnavailableError as error:
            log_failure("Required hardware dependency unavailable", error)
            self.exit_code = 69
        except ReservationSetupError as error:
            log_failure("Reservation storage unavailable", error, resource=error.resource, path=str(error.path))
            self.exit_code = 78
        except DeviceUnavailableError as error:
            log_failure("Camera or display unavailable", error)
            self.exit_code = 78
        except Exception as error:
            # See doc/wifi-qr-reader.md#secrets.
            log_failure("Reader operation failed", error, level=logging.ERROR)
            self.exit_code = 1
        finally:
            self.close()
        return self.exit_code


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    try:
        runtime_directory = Path(os.environ.get("RUNTIME_DIRECTORY", "/run/palmimo-wifi-qr"))
        allowed_uid = pwd.getpwnam(os.environ["PALMIMO_WIFI_QR_PEER_USER"]).pw_uid
    except KeyError as error:
        log_failure("Portal peer user is not configured or does not exist", error, level=logging.ERROR)
        return 1
    reader = Reader(runtime_directory, allowed_uid)
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: reader.stop())
    try:
        return reader.run()
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    raise SystemExit(main())
