import errno
import json
import os
import socket
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock

import pytest

from tests import test_wifi_qr_reader as reader_tests
from tests.test_wifi_qr_hardware import Serial, VideoDevice
from tests.test_wifi_qr_reader import Clock, Harness, receive


module = reader_tests.module
tmp_path = reader_tests.tmp_path
harness = reader_tests.harness


@pytest.mark.parametrize("error_code", [0, 1])
def test_jpeg_warning_preserves_pixels_and_fatal_discards_frame(module: Any, error_code: int) -> None:
    import ctypes

    def header(handle: Any, data: bytes, length: int, w: Any, h: Any, subsampling: Any, colorspace: Any) -> int:
        ctypes.cast(w, ctypes.POINTER(ctypes.c_int)).contents.value = 4
        ctypes.cast(h, ctypes.POINTER(ctypes.c_int)).contents.value = 4
        return 0

    def decode(
        handle: Any, data: bytes, length: int, output: Any, w: int, pitch: int, h: int, format_: int, flags: int
    ) -> int:
        ctypes.memset(output, 73, w * h)
        return -1

    library = SimpleNamespace(
        tjInitDecompress=Mock(return_value=123),
        tjDecompressHeader3=Mock(side_effect=header),
        tjDecompress2=Mock(side_effect=decode),
        tjGetErrorCode=Mock(return_value=error_code),
        tjDestroy=Mock(),
    )
    decoder = module.TurboJPEG(library)
    try:
        if error_code == 0:
            assert decoder.decode(b"jpeg", 2) == (b"I" * 4, 2, 2)
        else:
            with pytest.raises(ValueError):
                decoder.decode(b"jpeg", 2)
    finally:
        decoder.close()


def test_decode_failures_keep_scanning_and_success_resets_failure_limit(
    harness: Harness, caplog: pytest.LogCaptureFixture
) -> None:
    class Decoder:
        def decode(self, frame: list[str]) -> list[str]:
            if frame == ["broken"]:
                raise ValueError("WIFI:P:secret;;")
            return frame

    harness.reader.decoder = Decoder()
    with harness.connect() as client:
        receive(client)
        for frame in [["broken"]] * 99 + [[]] + [["broken"]] * 99:
            harness.camera.frames.append(frame)
            assert harness.reader.step()
        assert harness.display.screens[-1][0] == "scanning"
        harness.camera.frames.append(["valid"])
        harness.reader.step()
        assert receive(client) == [{"type": "qr", "text": "valid"}]
    assert "secret" not in caplog.text


def test_reader_stops_after_one_hundred_consecutive_decode_failures(harness: Harness) -> None:
    class Decoder:
        calls = 0

        def decode(self, frame: Any) -> list[str]:
            self.calls += 1
            raise ValueError("corrupt")

    decoder = Decoder()
    harness.reader.decoder = decoder
    harness.camera.frames.extend([[]] * 101)
    assert harness.reader.run() == 1
    assert decoder.calls == 100


@pytest.mark.parametrize("defect", ["flag", "size", "index"])
def test_invalid_camera_buffer_is_discarded_and_next_frame_remains_readable(
    module: Any, tmp_path: Path, defect: str
) -> None:
    video = VideoDevice()
    original = video.ioctl
    corrupted = False

    def ioctl(fd: int, request: int, argument: Any) -> None:
        nonlocal corrupted
        original(fd, request, argument)
        if request == 0xC0585611 and not corrupted:
            corrupted = True
            if defect == "flag":
                argument.flags = 0x40
            elif defect == "size":
                argument.bytesused = 4097
            else:
                argument.index = 9

    path = tmp_path / "video0"
    path.touch()
    camera = module.Camera(
        path, ioctl=ioctl, map_buffer=lambda fd, length, offset: memoryview(video.buffers[offset // 4096])
    )
    try:
        video.ready.append((0, b"broken"))
        with pytest.raises(ValueError):
            camera.read()
        video.ready.append((1, b"valid"))
        assert camera.read() == b"valid"
    finally:
        camera.close()


@pytest.mark.parametrize(
    "failure,code,number",
    [
        ("busy", 75, errno.EAGAIN),
        ("missing", 78, errno.ENOENT),
        ("permission", 78, errno.EACCES),
        ("flock_permission", 78, errno.EACCES),
        ("symlink", 78, errno.ELOOP),
    ],
)
def test_reservation_failures_distinguish_contention_from_setup(
    module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    failure: str,
    code: int,
    number: int,
) -> None:
    rig = Harness(module, tmp_path)
    locks = tmp_path / "locks"
    path = locks / "camera.lock"
    if failure != "missing":
        locks.mkdir()
    if failure in {"busy", "flock_permission"}:
        path.write_text(json.dumps({"pid": 123, "user": "holder", "app": "demo"}))
        monkeypatch.setattr(module.fcntl, "flock", Mock(side_effect=BlockingIOError(number, "busy")))
    elif failure == "permission":
        original_open = module.os.open

        def open_lock(path: Any, flags: int, *args: Any, **kwargs: Any) -> int:
            if str(path).endswith("camera.lock"):
                raise PermissionError(number, "denied")
            return original_open(path, flags, *args, **kwargs)

        monkeypatch.setattr(module.os, "open", open_lock)
    elif failure == "symlink":
        target = locks / "target"
        target.touch()
        path.symlink_to(target)
    rig.reader.reserve = lambda resource: module.reserve_resource(resource, locks)
    assert rig.reader.run() == code
    if code == 78:
        assert str(path) in caplog.text
        assert str(number) in caplog.text


def test_reservation_metadata_uses_numeric_uid_when_name_is_missing(
    module: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(module.pwd, "getpwuid", Mock(side_effect=KeyError))
    with module.reserve_resource("camera", tmp_path):
        assert json.loads((tmp_path / "camera.lock").read_text())["user"] == str(os.geteuid())


@pytest.mark.parametrize(
    "holder",
    [
        {"pid": True, "user": {"private": "secret"}, "app": "x" * 129},
        {"pid": "secret", "user": "x" * 129, "app": ["secret"]},
    ],
)
def test_reservation_holder_logs_only_valid_bounded_metadata(
    module: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    holder: dict[str, Any],
) -> None:
    rig = Harness(module, tmp_path)
    (tmp_path / "camera.lock").write_text(json.dumps(holder))
    monkeypatch.setattr(module.fcntl, "flock", Mock(side_effect=BlockingIOError(errno.EAGAIN, "busy")))
    rig.reader.reserve = lambda resource: module.reserve_resource(resource, tmp_path)
    assert rig.reader.run() == 75
    assert "secret" not in caplog.text
    assert "x" * 129 not in caplog.text
    assert "True" not in caplog.text


@pytest.mark.parametrize(
    "stage,number,code",
    [("start", errno.EBUSY, 75), ("read", errno.ENODEV, 78), ("read", errno.ENXIO, 78), ("read", errno.EIO, 78)],
)
def test_camera_errno_is_classified_at_start_and_during_capture(
    module: Any, tmp_path: Path, stage: str, number: int, code: int, caplog: pytest.LogCaptureFixture
) -> None:
    rig = Harness(module, tmp_path)
    video = VideoDevice()
    path = tmp_path / "video0"
    path.touch()

    def ioctl(fd: int, request: int, argument: Any) -> None:
        if request == (0xC0D05605 if stage == "start" else 0xC0585611):
            raise OSError(number, "WIFI:P:secret;;")
        video.ioctl(fd, request, argument)

    rig.reader.camera_factory = lambda: module.Camera(
        path, ioctl=ioctl, map_buffer=lambda fd, length, offset: memoryview(video.buffers[offset // 4096])
    )
    assert rig.reader.run() == code
    assert "secret" not in caplog.text
    assert str(number) in caplog.text
    assert any(getattr(record, "error_errno", None) == number for record in caplog.records)
    assert any(getattr(record, "error_type", None) == "OSError" for record in caplog.records)


@pytest.mark.parametrize("stage", ["open", "transfer"])
@pytest.mark.parametrize("number,code", [(errno.EIO, 78), (errno.EBUSY, 75)])
def test_serial_errno_distinguishes_busy_from_unavailable(
    module: Any, tmp_path: Path, stage: str, number: int, code: int, caplog: pytest.LogCaptureFixture
) -> None:
    class SerialException(OSError):  # noqa: N818
        pass

    def fail() -> Any:
        raise SerialException(number, "WIFI:P:secret;;")

    rig = Harness(module, tmp_path)
    if stage == "open":
        rig.reader.display_factory = lambda: module.Display(port_factory=fail)
    else:
        clock = Clock()
        serial = Serial(clock)
        display = module.Display(port_factory=lambda: serial, clock=clock)
        serial.on_data = fail
        display.live(bytes(168 * 168))
        display.thread.join(timeout=2)
        rig.reader.display_factory = lambda: display
    assert rig.reader.run() == code
    assert "secret" not in caplog.text
    assert any(getattr(record, "error_type", None) == "SerialException" for record in caplog.records)


def test_qr_detected_before_portal_connection_hides_preview_without_caching(harness: Harness) -> None:
    harness.camera.frames.append(["secret"])
    harness.reader.step()
    assert harness.display.screens[-1][0] == "checking"
    with harness.connect() as client:
        assert receive(client) == [{"type": "hello", "proto": 1}]
        harness.clock.now = 15
        harness.reader.step()
        assert harness.display.screens[-1][0] == "scanning"
        with pytest.raises(TimeoutError):
            client.recv(1)


def test_firmware_without_power_status_is_unavailable_without_power_changes(module: Any) -> None:
    clock = Clock()
    serial = Serial(clock)
    original = serial.write

    def write(line: bytes) -> int:
        if line == b"STAT\n":
            serial.writes.append(line)
            serial.incoming.append(b"ERR STAT (try LIST)\n")
            return len(line)
        return original(line)

    serial.write = write  # type: ignore[method-assign]
    client = module.FrameClient(serial, clock=clock)
    with pytest.raises(module.DeviceUnavailableError):
        client.initialize()
    before = list(serial.writes)
    client.restore()
    assert serial.writes == before
    assert not any(line.startswith(b"POWER ") for line in serial.writes)


def test_reader_closes_portal_connection_after_releasing_reservations(module: Any, tmp_path: Path) -> None:
    rig = Harness(module, tmp_path)
    client: socket.socket | None = None
    released: list[str] = []

    @contextmanager
    def reserve(resource: str) -> Iterator[None]:
        try:
            yield
        finally:
            released.append(resource)
            assert client is not None
            with pytest.raises(TimeoutError):
                client.recv(1)

    rig.reader.reserve = reserve
    rig.reader.start()
    try:
        client = rig.connect()
        receive(client)
        client.settimeout(0.01)
        rig.reader.close()
        assert released == ["display", "camera"]
        assert client.recv(1) == b""
    finally:
        rig.reader.close()
        if client is not None:
            client.close()


def test_display_start_logs_version_once(module: Any, caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level("INFO")
    clock = Clock()
    serial = Serial(clock)
    display = module.Display(port_factory=lambda: serial, clock=clock)
    display.close()
    records = [record for record in caplog.records if hasattr(record, "display_firmware")]
    assert len(records) == 1
    assert getattr(records[0], "display_firmware", None) == "test"
