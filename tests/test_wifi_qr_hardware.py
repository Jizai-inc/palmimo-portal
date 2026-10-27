import base64
import ctypes
import errno
import threading
from collections import deque
from pathlib import Path
from typing import Any

import pytest

from tests import test_wifi_qr_reader as reader_tests
from tests.test_wifi_qr_reader import Clock


module = reader_tests.module


@pytest.mark.parametrize(
    "name,size,offsets",
    [
        ("V4L2Format", 208, {"type": 0, "fmt": 8}),
        (
            "V4L2Buffer",
            88,
            {
                "index": 0,
                "bytesused": 8,
                "timestamp": 24,
                "timecode": 40,
                "sequence": 56,
                "memory": 60,
                "m": 64,
                "length": 72,
                "request_fd": 80,
            },
        ),
        ("V4L2RequestBuffers", 20, {"count": 0, "type": 4, "memory": 8}),
    ],
)
def test_v4l2_structures_match_linux_64bit_abi(module: Any, name: str, size: int, offsets: dict[str, int]) -> None:
    structure = getattr(module, name)
    assert ctypes.sizeof(structure) == size
    for field, offset in offsets.items():
        assert getattr(structure, field).offset == offset


@pytest.mark.parametrize(
    "width,height,expected",
    [(6, 4, bytes([18, 16, 6, 4])), (4, 6, bytes([16, 14, 8, 6])), (4, 4, bytes([12, 10, 4, 2]))],
)
def test_gray_preview_crops_center_scales_and_rotates(module: Any, width: int, height: int, expected: bytes) -> None:
    assert module.preview_gray(bytes(range(width * height)), width, height, size=2) == expected


@pytest.mark.parametrize("pixels,width,height", [(b"", 0, 1), (b"", 1, 0), (b"123", 2, 2)])
def test_gray_preview_rejects_inconsistent_pixels(module: Any, pixels: bytes, width: int, height: int) -> None:
    with pytest.raises(ValueError):
        module.preview_gray(pixels, width, height)


class Serial:
    def __init__(self, clock: Clock, power: str = "OFF", probes: list[bytes | None] | None = None) -> None:
        self.clock = clock
        self.power = power
        self.probes = deque(probes or [b"OK FRAME BEGIN\n"])
        self.incoming: deque[bytes] = deque()
        self.writes: list[bytes] = []
        self.threads: set[int] = set()
        self.total = 0
        self.corrupt_ack = False
        self.closed = False
        self.on_data: Any = None

    def write(self, line: bytes) -> int:
        self.threads.add(threading.get_ident())
        self.writes.append(line)
        if line == b"VERSION\n":
            self.incoming.append(b"VERSION test\n")
        elif line == b"STAT\n":
            self.incoming.extend([b"EVT POWER OFF\n", b"PONG\n", f"STAT up=10 power={self.power} fw=test\n".encode()])
        elif line.startswith(b"FRAME BEGIN "):
            self.total = 0
            response = self.probes.popleft() if self.probes else b"OK FRAME BEGIN\n"
            if response:
                self.incoming.append(response)
        elif line.startswith(b"FRAME DATA "):
            self.total += len(base64.b64decode(line.split()[2]))
            self.incoming.extend(
                [b"EVT POWER OFF\n", b"PONG\n", f"ACK {self.total - int(self.corrupt_ack)}\n".encode()]
            )
            self.corrupt_ack = False
            if self.on_data:
                self.on_data()
        elif line == b"FRAME END\n":
            self.incoming.append(b"OK FRAME\n")
        elif line == b"FRAME ABORT\n":
            self.incoming.append(b"OK FRAME ABORT\n")
        elif line == b"PING\n":
            self.incoming.append(b"PONG\n")
        elif line.strip():
            self.incoming.append(b"OK " + line.removeprefix(b"EXPR "))
        return len(line)

    def read_until(self, expected: bytes = b"\n", size: int = 4096) -> bytes:
        self.threads.add(threading.get_ident())
        if self.incoming:
            return self.incoming.popleft()
        self.clock.sleep(0.05)
        return b""

    def close(self) -> None:
        self.threads.add(threading.get_ident())
        self.closed = True


@pytest.mark.parametrize("power", ["ON", "OFF"])
def test_frame_client_probes_version_and_frames_then_restores_power(module: Any, power: str) -> None:
    clock = Clock()
    serial = Serial(clock, power)
    client = module.FrameClient(serial, clock=clock)
    client.initialize()
    assert serial.writes[0] == b"\n"
    assert serial.writes[1] == b"VERSION\n"
    assert b"STAT\n" in serial.writes
    client.restore()
    assert serial.writes[-1] == (b"IDLE\n" if power == "ON" else b"EXPR SLEEPING\n")


def test_frame_probe_retries_after_unrecognized_response(module: Any) -> None:
    clock = Clock()
    serial = Serial(clock, power="ON", probes=[b"ERR parse\n", b"OK FRAME BEGIN\n"])
    client = module.FrameClient(serial, clock=clock)
    client.initialize()
    assert serial.writes.count(b"\n") == 2


@pytest.mark.parametrize("line,expected", [(b"STAT power=ON up=3", True), (b"STAT up=0 power=OFF fw=x", False)])
def test_stat_parser_reads_power_field(module: Any, line: bytes, expected: bool) -> None:
    assert module.parse_power(line) is expected


@pytest.mark.parametrize(
    "line", [b"STAT power=MAYBE", b"STAT no_power=ON", b"EVT power=ON", b"STAT power=ON power=OFF"]
)
def test_stat_parser_rejects_unknown_or_ambiguous_power(module: Any, line: bytes) -> None:
    with pytest.raises(module.DisplayProtocolError):
        module.parse_power(line)


@pytest.mark.parametrize("fmt,bpp", [("GRAY8", 1), ("RGB565", 2)])
def test_frame_transfer_encodes_binary_in_bounded_lines_with_cumulative_acks(module: Any, fmt: str, bpp: int) -> None:
    clock = Clock()
    serial = Serial(clock)
    client = module.FrameClient(serial, clock=clock)
    pixels = bytes(range(256)) * (16 * bpp)
    client.send_frame(pixels, 64, 64, fmt)
    assert serial.writes[0] == f"FRAME BEGIN 64 64 {fmt}\n".encode()
    chunks = [line for line in serial.writes if line.startswith(b"FRAME DATA ")]
    assert all(len(line) <= 2063 for line in chunks)
    assert b"".join(base64.b64decode(line.split()[2]) for line in chunks) == pixels
    assert serial.writes[-1] == b"FRAME END\n"


def test_frame_transfer_aborts_and_restarts_after_wrong_ack(module: Any) -> None:
    clock = Clock()
    serial = Serial(clock)
    serial.corrupt_ack = True
    client = module.FrameClient(serial, clock=clock)
    client.send_frame(b"\x81\xff\x03\x0a", 2, 2, "GRAY8")
    assert serial.writes == [
        b"FRAME BEGIN 2 2 GRAY8\n",
        b"FRAME DATA gf8DCg==\n",
        b"FRAME ABORT\n",
        b"FRAME BEGIN 2 2 GRAY8\n",
        b"FRAME DATA gf8DCg==\n",
        b"FRAME END\n",
    ]


def test_frame_transfer_cancellation_aborts_before_more_pixels(module: Any) -> None:
    clock = Clock()
    serial = Serial(clock)
    cancelled = False

    def cancel() -> None:
        nonlocal cancelled
        cancelled = True

    serial.on_data = cancel
    client = module.FrameClient(serial, clock=clock)
    assert not client.send_frame(bytes(4096), 64, 64, "GRAY8", cancelled=lambda: cancelled)
    assert serial.writes[-1] == b"FRAME ABORT\n"
    assert sum(line.startswith(b"FRAME DATA ") for line in serial.writes) == 1


def test_ascii_font_draws_expected_pixels_and_clips(module: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(module, "ASCII_FONT", {"A": (0x40, 0xA0, 0xE0)})
    canvas = bytearray(4 * 3 * 2)
    module.draw_ascii(canvas, 4, 3, "A", 1, 0, color=0xF800)
    red = b"\x00\xf8"
    black = b"\0\0"
    assert bytes(canvas) == black * 2 + red + black + black + red + black + red + black + red * 3
    module.draw_ascii(canvas, 4, 3, "A", -1, -1, color=0x07E0)
    assert canvas[:2] == black
    assert canvas[2:4] == b"\xe0\x07"
    assert canvas[8:12] == b"\xe0\x07" * 2


@pytest.mark.parametrize(
    "state,fields",
    [
        ("checking", {}),
        ("connecting", {}),
        ("connected", {}),
    ]
    + [
        (state, {"reason": reason})
        for state in ("rejected", "failed")
        for reason in (
            "not_wifi",
            "unsupported_security",
            "hidden_network",
            "invalid_ssid",
            "invalid_psk",
            "busy",
            "connect_failed",
        )
    ],
)
def test_state_screen_assets_have_complete_rgb565_pixels(module: Any, state: str, fields: dict[str, str]) -> None:
    pixels = module.state_screen(state, fields)
    assert len(pixels) == 168 * 168 * 2
    assert len(set(pixels)) > 2


def test_connecting_omits_non_ascii_ssid(module: Any) -> None:
    baseline = module.state_screen("connecting", {})
    assert module.state_screen("connecting", {"ssid": "日本語"}) == baseline
    assert module.state_screen("connecting", {"ssid": "office"}) != baseline


class VideoDevice:
    def __init__(self) -> None:
        self.queued: list[int] = []
        self.ready: deque[tuple[int, bytes]] = deque()
        self.buffers = [bytearray(4096) for _ in range(3)]
        self.streaming = False

    def ioctl(self, fd: int, request: int, argument: Any) -> None:
        if request == 0xC0D05605:
            assert bytes(argument)[8:24] == b"\x20\x0a\x00\x00\x98\x07\x00\x00MJPG\x00\x00\x00\x00"
        elif request == 0xC0145608:
            argument.count = 3
        elif request == 0xC0585609:
            argument.length = 4096
            argument.m.offset = argument.index * 4096
        elif request == 0xC058560F:
            self.queued.append(argument.index)
        elif request == 0x40045612:
            self.streaming = True
        elif request == 0x40045613:
            self.streaming = False
        elif request == 0xC0585611:
            if not self.ready:
                raise BlockingIOError(errno.EAGAIN, "empty")
            index, pixels = self.ready.popleft()
            self.buffers[index][: len(pixels)] = pixels
            argument.index = index
            argument.bytesused = len(pixels)
        else:
            pytest.fail(f"Unexpected ioctl {request:x}")


def test_camera_returns_latest_frame_and_requeues_all_buffers(module: Any, tmp_path: Path) -> None:
    video = VideoDevice()
    path = tmp_path / "video0"
    path.touch()
    camera = module.Camera(
        path, ioctl=video.ioctl, map_buffer=lambda fd, length, offset: memoryview(video.buffers[offset // 4096])
    )
    try:
        assert video.streaming
        assert video.queued == [0, 1, 2]
        video.ready.extend([(0, b"old"), (1, b"new"), (2, b"latest")])
        assert camera.read() == b"latest"
        assert video.queued == [0, 1, 2, 0, 1, 2]
        assert camera.read() is None
    finally:
        camera.close()
    assert not video.streaming


def test_decoder_scans_both_grayscale_scales_and_only_enables_qr(module: Any) -> None:
    from types import SimpleNamespace

    class JPEG:
        def __init__(self) -> None:
            self.denominators: list[int] = []
            self.closed = False

        def decode(self, frame: bytes, denominator: int) -> tuple[bytes, int, int]:
            self.denominators.append(denominator)
            side = 4 if denominator == 4 else 8
            return bytes(range(side * side)), side, side

        def close(self) -> None:
            self.closed = True

    class Scanner:
        def __init__(self) -> None:
            self.configs: list[str] = []
            self.dimensions: list[tuple[int, int]] = []

        def parse_config(self, config: str) -> None:
            self.configs.append(config)

        def scan(self, image: Any) -> None:
            self.dimensions.append((image.width, image.height))

    class Image(list[Any]):
        def __init__(self, width: int, height: int, format_: str, data: bytes) -> None:
            assert format_ == "Y800"
            assert len(data) == width * height
            assert data == bytes(range(width * height))
            self.width, self.height = width, height
            super().__init__([SimpleNamespace(data=b"same"), SimpleNamespace(data="quarter" if width == 4 else "half")])

    jpeg = JPEG()
    scanner = Scanner()
    decoder = module.Decoder(jpeg=jpeg, zbar_module=SimpleNamespace(ImageScanner=lambda: scanner, Image=Image))
    try:
        assert decoder.decode(b"jpeg") == ["same", "quarter", "half"]
        assert jpeg.denominators == [4, 2]
        assert scanner.dimensions == [(4, 4), (8, 8)]
        assert scanner.configs == ["disable", "qrcode.enable"]
        assert len(decoder.preview) == 168 * 168
        assert decoder.preview[0] == 15
        assert decoder.preview[-1] == 0
    finally:
        decoder.close()
    assert jpeg.closed


@pytest.mark.parametrize("denominator,width,height", [(4, 648, 486), (2, 1296, 972)])
def test_turbojpeg_binding_requests_scaled_gray_pixels(module: Any, denominator: int, width: int, height: int) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    def header(handle: Any, data: bytes, length: int, w: Any, h: Any, subsampling: Any, colorspace: Any) -> int:
        assert data == b"jpeg" and length == 4
        ctypes.cast(w, ctypes.POINTER(ctypes.c_int)).contents.value = 2592
        ctypes.cast(h, ctypes.POINTER(ctypes.c_int)).contents.value = 1944
        return 0

    def decompress(
        handle: Any, data: bytes, length: int, pixels: Any, w: int, pitch: int, h: int, format_: int, flags: int
    ) -> int:
        assert (w, h, pitch, format_) == (width, height, 0, 6)
        ctypes.memset(pixels, 127, w * h)
        return 0

    library = SimpleNamespace(
        tjInitDecompress=Mock(return_value=0x100000001),
        tjDecompressHeader3=Mock(side_effect=header),
        tjDecompress2=Mock(side_effect=decompress),
        tjGetErrorCode=Mock(return_value=1),
        tjDestroy=Mock(return_value=0),
    )
    jpeg = module.TurboJPEG(library)
    assert jpeg.decode(b"jpeg", denominator) == (b"\x7f" * width * height, width, height)
    jpeg.close()
    library.tjDestroy.assert_called_once_with(0x100000001)


class SlowSerial(Serial):
    def __init__(self, clock: Clock) -> None:
        super().__init__(clock)
        self.waiting = threading.Event()
        self.release = threading.Event()
        self.completed = threading.Event()
        self.block_once = False
        self.frame_format = ""
        self.frame_pixels = bytearray()
        self.finished: list[tuple[str, bytes]] = []

    def write(self, line: bytes) -> int:
        if line.startswith(b"FRAME BEGIN "):
            self.frame_format = line.split()[4].decode()
            self.frame_pixels.clear()
        elif line.startswith(b"FRAME DATA "):
            self.frame_pixels.extend(base64.b64decode(line.split()[2]))
        elif line == b"FRAME END\n":
            self.finished.append((self.frame_format, bytes(self.frame_pixels)))
            if self.frame_format == "RGB565" or self.frame_pixels[:1] == b"\x03":
                self.completed.set()
        return super().write(line)

    def read_until(self, expected: bytes = b"\n", size: int = 4096) -> bytes:
        if self.block_once:
            self.block_once = False
            self.waiting.set()
            if not self.release.wait(2):
                raise TimeoutError("Test serial was not released")
        return super().read_until(expected, size)


def test_live_preview_shows_centered_wifi_prompt_inside_face_without_changing_camera_pixels(module: Any) -> None:
    serial = SlowSerial(Clock())
    display = module.Display(port_factory=lambda: serial, clock=serial.clock)
    frame = b"\x03" * (168 * 168)
    serial.finished.clear()
    try:
        display.live(frame)
        assert serial.completed.wait(2)
        display.check()
        format_, pixels = serial.finished[-1]
        assert format_ == "GRAY8"
        assert len(pixels) == len(frame)
        assert pixels[: 154 * 168] == frame[: 154 * 168]
        assert frame == b"\x03" * (168 * 168)
        assert set(pixels[154 * 168 :]) == {0, 255}
        assert all(
            30 <= index % 168 < 138 and 156 <= index // 168 < 166
            for index in range(154 * 168, len(pixels))
            if pixels[index] == 255
        )
        expected_s = (
            "000000",
            "000000",
            "001110",
            "010000",
            "001000",
            "000110",
            "000010",
            "001110",
            "000000",
            "000000",
        )
        expected_r = (
            "000000",
            "000000",
            "011100",
            "010010",
            "010010",
            "011100",
            "010100",
            "010010",
            "000000",
            "000000",
        )
        for left, glyph in ((30, expected_s), (132, expected_r)):
            for row, bits in enumerate(glyph):
                assert pixels[(156 + row) * 168 + left : (156 + row) * 168 + left + 6] == bytes(
                    255 if bit == "1" else 0 for bit in bits
                )
        assert pixels[154 * 168 : 156 * 168] == bytes(2 * 168)
        assert pixels[166 * 168 :] == bytes(2 * 168)
        for top, bottom, left, right in ((154, 168, 0, 168), (156, 166, 30, 138)):
            for x in (left, right):
                for y in (top, bottom):
                    assert (72 + 2 * x - 240) ** 2 + (72 + 2 * y - 240) ** 2 <= 240**2
    finally:
        display.close()


@pytest.mark.parametrize("switch_screen", [False, True])
def test_display_worker_drops_stale_previews_and_prioritizes_screen_without_sharing_port(
    module: Any, switch_screen: bool
) -> None:
    clock = Clock()
    serial = SlowSerial(clock)
    opened_on: list[int] = []

    def open_port() -> Serial:
        opened_on.append(threading.get_ident())
        return serial

    display = module.Display(port_factory=open_port, clock=clock)
    serial.finished.clear()
    serial.on_data = lambda: setattr(serial, "block_once", not serial.waiting.is_set())
    try:
        display.live(b"\x01" * (168 * 168))
        assert serial.waiting.wait(1)
        display.live(b"\x02" * (168 * 168))
        display.live(b"\x03" * (168 * 168))
        if switch_screen:
            display.screen("checking", {})
        cut = len(serial.writes)
        display.ping()
        serial.release.set()
        assert serial.completed.wait(2)
        display.check()
        if switch_screen:
            assert serial.writes[cut] == b"FRAME ABORT\n"
            assert not any(line.startswith(b"FRAME BEGIN 168 168 GRAY8") for line in serial.writes[cut:])
            assert serial.finished[-1][0] == "RGB565"
        else:
            assert [(format_, pixels[: 154 * 168]) for format_, pixels in serial.finished] == [
                ("GRAY8", b"\x01" * (154 * 168)),
                ("GRAY8", b"\x03" * (154 * 168)),
            ]
        assert b"PING\n" in serial.writes
    finally:
        serial.release.set()
        display.close()
    assert serial.closed
    assert serial.threads == {opened_on[0]}
    assert opened_on[0] != threading.get_ident()


@pytest.mark.parametrize(
    "state,fields",
    [
        ("scanning", {}),
        ("checking", {}),
        ("connecting", {"ssid": "office"}),
        ("rejected", {"reason": "not_wifi"}),
        ("failed", {"reason": "connect_failed"}),
        ("connected", {"url": "http://robot.local", "qr": {"size": 2, "rows": ["10", "01"]}}),
    ],
)
def test_display_worker_renders_each_state_as_frames(module: Any, state: str, fields: dict[str, Any]) -> None:
    clock = Clock()
    serial = Serial(clock, power="ON")
    display = module.Display(port_factory=lambda: serial, clock=clock)
    sent = threading.Event()
    original_write = serial.write

    def write(line: bytes) -> int:
        result = original_write(line)
        if line == b"FRAME END\n":
            sent.set()
        return result

    serial.write = write  # type: ignore[method-assign]
    before = len(serial.writes)
    try:
        display.screen(state, fields)
        assert sent.wait(1)
        display.check()
        format_ = "GRAY8" if state == "scanning" else "RGB565"
        assert f"FRAME BEGIN 168 168 {format_}\n".encode() in serial.writes[before:]
    finally:
        display.close()
    assert not any(line.startswith(b"EXPR") for line in serial.writes)
    assert serial.writes[-1] == b"IDLE\n"


def test_display_worker_surfaces_serial_loss_without_leaking_exception(module: Any) -> None:
    clock = Clock()
    serial = Serial(clock)
    display = module.Display(port_factory=lambda: serial, clock=clock)
    failed = threading.Event()

    def fail() -> None:
        failed.set()
        raise OSError("WIFI:P:secret;;")

    serial.on_data = fail
    try:
        display.live(bytes(168 * 168))
        assert failed.wait(1)
        display.thread.join(timeout=2)
        with pytest.raises(RuntimeError) as error:
            display.check()
        assert "secret" not in str(error.value)
    finally:
        display.close()


@pytest.mark.parametrize("failure_request", [0xC0585609, 0x40045612])
def test_camera_initialization_failure_closes_fd_and_mappings(
    module: Any, tmp_path: Path, failure_request: int
) -> None:
    import os

    video = VideoDevice()
    path = tmp_path / "video0"
    path.touch()
    descriptors: list[int] = []
    mappings: list[memoryview] = []

    def ioctl(fd: int, request: int, argument: Any) -> None:
        descriptors.append(fd)
        if request == failure_request:
            raise OSError(errno.ENODEV, "camera disconnected")
        video.ioctl(fd, request, argument)

    def map_buffer(fd: int, length: int, offset: int) -> memoryview:
        mapping = memoryview(video.buffers[offset // 4096])
        mappings.append(mapping)
        return mapping

    with pytest.raises(module.DeviceUnavailableError):
        module.Camera(path, ioctl=ioctl, map_buffer=map_buffer)
    with pytest.raises(OSError):
        os.fstat(descriptors[0])
    for mapping in mappings:
        with pytest.raises(ValueError):
            len(mapping)


@pytest.mark.parametrize("vid,accepted", [(None, False), (0x10C4, False), (0x2E8A, True)])
def test_display_port_selection_requires_pico_vid(
    module: Any, monkeypatch: pytest.MonkeyPatch, vid: int | None, accepted: bool
) -> None:
    from types import SimpleNamespace
    from unittest.mock import Mock

    serial = SimpleNamespace(Serial=Mock(return_value="port"))
    ports = SimpleNamespace(comports=lambda: [SimpleNamespace(vid=vid, device="/dev/ttyACM0")])
    monkeypatch.setattr(module.importlib, "import_module", lambda name: serial if name == "serial" else ports)
    if accepted:
        assert module.open_display_port() == "port"
        serial.Serial.assert_called_once_with("/dev/ttyACM0", 115200, timeout=0.05, write_timeout=0.2)
    else:
        with pytest.raises(module.DeviceUnavailableError):
            module.open_display_port()
        serial.Serial.assert_not_called()


@pytest.mark.parametrize("power,command", [("ON", b"IDLE\n"), ("OFF", b"EXPR SLEEPING\n")])
def test_unacknowledged_restore_still_closes_display_port(module: Any, power: str, command: bytes) -> None:
    clock = Clock()
    serial = Serial(clock, power=power)
    display = module.Display(port_factory=lambda: serial, clock=clock)
    original_write = serial.write

    def write(line: bytes) -> int:
        result = original_write(line)
        if line == command:
            serial.incoming.clear()
        return result

    serial.write = write  # type: ignore[method-assign]
    display.close()
    assert serial.closed
    assert serial.writes[-1] == command
    assert not any(line.startswith(b"POWER ") for line in serial.writes)
    with pytest.raises(RuntimeError):
        display.check()


def test_heartbeat_consumes_reply_before_next_command(module: Any) -> None:
    clock = Clock()
    serial = Serial(clock)
    client = module.FrameClient(serial, clock=clock)
    for _ in range(3):
        client.ping()
        assert not serial.incoming
