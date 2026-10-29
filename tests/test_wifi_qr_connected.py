import struct
import threading
import time
from contextlib import suppress
from pathlib import Path
from typing import Any

import pytest

from tests import test_wifi_qr_reader as reader_tests
from tests.test_wifi_qr_hardware import Serial
from tests.test_wifi_qr_reader import Clock, Harness, receive


module = reader_tests.module
tmp_path = reader_tests.tmp_path
harness = reader_tests.harness
QR = {"size": 2, "rows": ["10", "01"]}


def pixel(pixels: bytes, x: int, y: int) -> int:
    return struct.unpack_from("<H", pixels, (y * 168 + x) * 2)[0]


def test_connected_qr_renders_modules_and_four_module_quiet_zone(module: Any) -> None:
    pixels = module.state_screen("connected", {"url": "http://robot.local", "qr": QR})
    assert pixel(pixels, 75, 76) == 0
    assert pixel(pixels, 83, 84) == 0
    assert pixel(pixels, 84, 76) == 0xFFFF
    assert pixel(pixels, 75, 85) == 0xFFFF
    assert pixel(pixels, 84, 85) == 0
    assert pixel(pixels, 92, 93) == 0
    assert pixel(pixels, 39, 40) == 0xFFFF
    assert pixel(pixels, 74, 75) == 0xFFFF
    assert pixel(pixels, 93, 94) == 0xFFFF
    assert pixel(pixels, 128, 129) == 0xFFFF


@pytest.mark.parametrize("length,truncated", [(28, False), (29, True), (80, True)])
def test_connected_url_uses_six_pixel_cells_and_ellipsis(module: Any, length: int, truncated: bool) -> None:
    pixels = module.state_screen("connected", {"url": "a" * length, "qr": QR})
    expected_a = ("000000", "000000", "000000", "011100", "000010", "011110", "100010", "011110", "000000", "000000")
    expected_ellipsis = ("000000",) * 7 + ("101010",) + ("000000",) * 2
    for left, glyph in ((156, expected_a), (162, expected_ellipsis if truncated else expected_a)):
        for row, bits in enumerate(glyph):
            assert [pixel(pixels, left + column, 26 + row) == 0xFFFF for column in range(6)] == [
                bit == "1" for bit in bits
            ]


@pytest.mark.parametrize(
    "qr",
    [
        {"size": True, "rows": ["1"]},
        {"size": 90, "rows": ["0" * 90] * 90},
        {"size": 2, "rows": ["10"]},
        {"size": 2, "rows": ["10", "0x"]},
        {"size": 2, "rows": ["10", 1]},
    ],
)
def test_connected_rejects_invalid_qr_without_changing_screen(harness: Harness, qr: Any) -> None:
    with harness.connect() as client:
        receive(client)
        harness.message(client, state="connected", url="http://robot.local", qr=qr)
        assert harness.display.screens[-1][0] == "scanning"


class TapSerial(Serial):
    def __init__(self, clock: Clock, early_tap: bytes | None = None) -> None:
        super().__init__(clock)
        self.early_tap = early_tap
        self.format = ""
        self.rendered = threading.Event()
        self.tap_read = threading.Event()

    @property
    def in_waiting(self) -> int:
        return len(self.incoming)

    def write(self, line: bytes) -> int:
        if line.startswith(b"FRAME BEGIN "):
            self.format = line.split()[4].decode()
        if line == b"FRAME END\n" and self.format == "RGB565" and self.early_tap:
            self.incoming.append(self.early_tap)
        return super().write(line)

    def read_until(self, expected: bytes = b"\n", size: int = 4096) -> bytes:
        line = super().read_until(expected, size)
        if line == b"OK FRAME\n" and self.format == "RGB565":
            self.rendered.set()
        if line.startswith(b"EVT TAP "):
            self.tap_read.set()
        return line


@pytest.mark.parametrize(
    "state,timing,point,expected",
    [
        ("connected", "after", (172, 336), True),
        ("connected", "after", (307, 369), True),
        ("connected", "after", (171, 336), False),
        ("connected", "after", (308, 336), False),
        ("connected", "after", (172, 370), False),
        ("checking", "after", (172, 336), False),
        ("connected", "before", (172, 336), False),
        pytest.param("connected", "after", (10**400, 336), False, id="outside-frame"),
    ],
)
def test_close_tap_is_sent_once_only_for_rendered_connected_button(
    module: Any, tmp_path: Path, state: str, timing: str, point: tuple[int, int], expected: bool
) -> None:
    tap = f"EVT TAP {point[0]} {point[1]}\n".encode()
    clock = Clock()
    serial = TapSerial(clock, tap if timing == "before" else None)
    display = module.Display(port_factory=lambda: serial, clock=clock)
    rig = Harness(module, tmp_path)
    rig.reader.display_factory = lambda: display
    rig.reader.start()
    try:
        with rig.connect() as client:
            assert receive(client) == [{"type": "hello", "proto": 1}]
            fields = {"url": "http://robot.local", "qr": QR} if state == "connected" else {}
            rig.message(client, state=state, **fields)
            assert serial.rendered.wait(2)
            if timing == "after":
                serial.incoming.extend([tap, tap])
            assert serial.tap_read.wait(2)
            messages = []
            client.settimeout(0.01)
            deadline = time.monotonic() + 0.2
            while time.monotonic() < deadline:
                rig.reader.step()
                with suppress(TimeoutError):
                    messages.extend(receive(client))
            assert messages == ([{"type": "close"}] if expected else [])
    finally:
        rig.reader.close()


@pytest.mark.parametrize(
    "version_reply,replies,code",
    [
        (b"VERSION test\n", [b"ERR FRAME (try LIST)\n"], 76),
        (b"ERR VERSION (try LIST)\n", [b"ERR FRAME (try LIST)\n"], 76),
        (None, [b"ERR FRAME (try LIST)\n"], 76),
        (b"VERSION test\n", [b"ERR FRAME ARGS\n", b"ERR FRAME (try LIST)\n"], 76),
        (b"VERSION test\n", [None, None], 78),
        (None, [None, None], 78),
    ],
)
def test_unsupported_or_silent_firmware_exits_without_display_commands(
    module: Any, tmp_path: Path, version_reply: bytes | None, replies: list[bytes | None], code: int
) -> None:
    clock = Clock()
    serial = Serial(clock, probes=replies)
    original = serial.write

    def write(line: bytes) -> int:
        if line == b"VERSION\n":
            serial.writes.append(line)
            if version_reply is not None:
                serial.incoming.append(version_reply)
            return len(line)
        return original(line)

    serial.write = write  # type: ignore[method-assign]
    rig = Harness(module, tmp_path)
    rig.reader.display_factory = lambda: module.Display(port_factory=lambda: serial, clock=clock)
    assert rig.reader.run() == code
    assert serial.closed
    assert not any(
        line.startswith((b"FRAME DATA", b"FRAME END", b"FRAME ABORT", b"EXPR", b"POWER", b"IDLE"))
        for line in serial.writes
    )


@pytest.mark.parametrize("delay", [0.35, 0.95])
def test_frame_ack_tolerates_firmware_reinitialization_pause(module: Any, delay: float) -> None:
    clock = Clock()
    serial = Serial(clock)
    original = serial.read_until

    available_at: float | None = None

    def read_until(expected: bytes = b"\n", size: int = 4096) -> bytes:
        nonlocal available_at
        if serial.incoming and serial.incoming[0].startswith(b"ACK "):
            if available_at is None:
                available_at = clock() + delay
            if clock() < available_at:
                clock.sleep(min(0.05, available_at - clock()))
                return b""
            available_at = None
        return original(expected, size)

    serial.read_until = read_until  # type: ignore[method-assign]
    client = module.FrameClient(serial, clock=clock)
    assert client.send_frame(bytes(4), 2, 2, "GRAY8")
    assert b"FRAME ABORT\n" not in serial.writes
