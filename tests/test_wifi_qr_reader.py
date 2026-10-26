import importlib.util
import json
import os
import socket
import sys
import tempfile
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest


READER_PATH = Path(__file__).resolve().parents[1] / "palmimo_portal/wifi_qr_reader/reader.py"


@pytest.fixture
def tmp_path() -> Iterator[Path]:
    # Darwin limits Unix socket paths to 104 bytes.
    with tempfile.TemporaryDirectory(prefix="qr-", dir="/tmp") as directory:
        yield Path(directory)


@pytest.fixture
def module() -> Any:
    spec = importlib.util.spec_from_file_location("isolated_wifi_qr_reader", READER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class Clock:
    now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Camera:
    def __init__(self) -> None:
        self.frames: deque[list[str]] = deque()
        self.closed = False

    def read(self) -> list[str] | None:
        return self.frames.popleft() if self.frames else None

    def close(self) -> None:
        self.closed = True


class Decoder:
    def decode(self, frame: list[str]) -> list[str]:
        return frame


class Display:
    def __init__(self, mode: str = "frame") -> None:
        self.mode = mode
        self.events: list[Any] = []
        self.screens: list[tuple[str, dict[str, Any]]] = []

    def screen(self, state: str, fields: dict[str, Any]) -> None:
        self.events.append(state)
        self.screens.append((state, fields))

    def live(self, frame: Any) -> None:
        self.events.append(("live", frame))

    def ping(self) -> None:
        self.events.append("ping")

    def close(self) -> None:
        self.events.append("sleeping")


class Harness:
    def __init__(self, module: Any, tmp_path: Path, mode: str = "frame") -> None:
        self.clock = Clock()
        self.camera = Camera()
        self.display = Display(mode)
        self.events: list[str] = []
        self.uid = os.getuid()
        self.reader = module.Reader(
            tmp_path,
            self.uid,
            clock=self.clock,
            sleep=self.clock.sleep,
            reserve=self.reserve,
            load_dependencies=lambda: self.events.append("dependencies"),
            camera_factory=lambda: self.camera,
            decoder_factory=Decoder,
            display_factory=lambda: self.display,
            peer_uid_of=lambda connection: self.uid,
        )

    @contextmanager
    def reserve(self, resource: str) -> Iterator[None]:
        assert self.reader.socket_path.is_socket()
        self.events.append(resource)
        try:
            yield
        finally:
            self.events.append(f"release:{resource}")

    def connect(self) -> socket.socket:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        client.settimeout(0.2)
        client.connect(str(self.reader.socket_path))
        self.reader.step()
        return client

    def message(self, client: socket.socket, **fields: Any) -> None:
        client.sendall(json.dumps({"type": "screen", **fields}).encode() + b"\n")
        self.reader.step()


def receive(client: socket.socket) -> list[Any]:
    return [json.loads(line) for line in client.recv(65536).splitlines()]


@pytest.fixture
def harness(module: Any, tmp_path: Path) -> Iterator[Harness]:
    harness = Harness(module, tmp_path)
    harness.reader.start()
    try:
        yield harness
    finally:
        harness.reader.close()


def test_reader_directory_contains_only_standalone_script() -> None:
    assert sorted(path.name for path in READER_PATH.parent.rglob("*.py")) == ["reader.py"]


@pytest.mark.parametrize("mode", ["frame", "expr"])
def test_startup_opens_socket_before_reservations_and_sends_ready_hello(module: Any, tmp_path: Path, mode: str) -> None:
    harness = Harness(module, tmp_path, mode)
    harness.reader.start()
    try:
        assert harness.events == ["camera", "display", "dependencies"]
        assert harness.reader.socket_path.stat().st_mode & 0o777 == 0o666
        with harness.connect() as client:
            assert receive(client) == [{"type": "hello", "proto": 1, "display": mode}]
    finally:
        harness.reader.close()
    assert harness.camera.closed
    assert harness.display.events[-1] == "sleeping"
    assert harness.events[-2:] == ["release:display", "release:camera"]
    assert not harness.reader.socket_path.exists()


def test_socket_rejects_other_uid_without_claiming_client(harness: Harness) -> None:
    harness.uid += 1
    with harness.connect() as rejected:
        assert rejected.recv(1) == b""
    harness.uid -= 1
    with harness.connect() as accepted:
        assert receive(accepted)[0]["type"] == "hello"


def test_socket_rejects_second_authenticated_client(harness: Harness) -> None:
    with harness.connect() as first, harness.connect() as second:
        assert receive(first)[0]["type"] == "hello"
        assert second.recv(1) == b""


def test_reader_exits_zero_after_thirty_seconds_without_authenticated_client(harness: Harness) -> None:
    harness.uid += 1
    with harness.connect():
        pass
    harness.clock.now = 29.99
    assert harness.reader.step()
    harness.clock.now = 30
    assert not harness.reader.step()
    assert harness.reader.exit_code == 0


@pytest.mark.parametrize(
    "failure,code", [("camera_lock", 75), ("display_lock", 75), ("dependencies", 69), ("camera", 78), ("display", 78)]
)
def test_startup_failure_returns_classified_exit_and_releases_resources(
    module: Any, tmp_path: Path, failure: str, code: int
) -> None:
    harness = Harness(module, tmp_path)

    def fail() -> Any:
        if failure == "dependencies":
            raise ImportError("unavailable")
        raise module.DeviceUnavailableError("unavailable")

    original_reserve = harness.reader.reserve

    @contextmanager
    def reserve(resource: str) -> Iterator[None]:
        if failure == f"{resource}_lock":
            raise module.ReservationUnavailableError(resource, {"pid": 123, "user": "holder", "app": "demo"})
        with original_reserve(resource):
            yield

    harness.reader.reserve = reserve
    if failure == "dependencies":
        harness.reader.load_dependencies = fail
    elif failure in {"camera", "display"}:
        setattr(harness.reader, f"{failure}_factory", fail)
    assert harness.reader.run() == code
    assert not harness.reader.socket_path.exists()
    if failure == "display":
        assert harness.camera.closed
    if failure != "camera_lock":
        assert "release:camera" in harness.events


def test_qr_hides_live_frame_before_sending_once(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    secret = "WIFI:S:home;P:secret;;"
    with harness.connect() as client:
        receive(client)
        harness.camera.frames.append([secret, "other"])
        harness.reader.step()
        assert harness.display.events[-1] == "checking"
        assert not any(isinstance(event, tuple) for event in harness.display.events)
        assert receive(client) == [{"type": "qr", "text": secret}]
        harness.reader.step()
        with pytest.raises(TimeoutError):
            client.recv(1)
        assert secret not in caplog.text


def test_qr_is_suppressed_until_thirty_consecutive_absent_frames(harness: Harness) -> None:
    with harness.connect() as client:
        receive(client)
        harness.camera.frames.append(["secret"])
        harness.reader.step()
        receive(client)
        harness.message(client, state="scanning")
        for texts in [["secret"]] + [[]] * 29 + [["secret"]] + [[]] * 29 + [["secret"]]:
            harness.camera.frames.append(texts)
            harness.reader.step()
        with pytest.raises(TimeoutError):
            client.recv(1)
        for _ in range(30):
            harness.camera.frames.append([])
            harness.reader.step()
        harness.camera.frames.append(["secret"])
        harness.reader.step()
        assert receive(client) == [{"type": "qr", "text": "secret"}]


def test_checking_returns_to_scanning_after_fifteen_seconds_without_screen(harness: Harness) -> None:
    with harness.connect() as client:
        receive(client)
        harness.message(client, state="checking")
        harness.clock.now = 14.99
        harness.reader.step()
        assert harness.display.events[-1] != "scanning"
        harness.clock.now = 15
        harness.reader.step()
        assert harness.display.events[-1] == "scanning"


@pytest.mark.parametrize(
    "state,fields",
    [
        ("scanning", {}),
        ("checking", {}),
        ("rejected", {"reason": "not_wifi"}),
        ("connecting", {"ssid": "home"}),
        ("failed", {"reason": "connect_failed"}),
        ("connected", {"hostname": "palmimo"}),
    ],
)
def test_socket_screen_updates_display(harness: Harness, state: str, fields: dict[str, str]) -> None:
    with harness.connect() as client:
        receive(client)
        harness.message(client, state=state, **fields)
        assert harness.display.screens[-1] == (state, fields)
        harness.clock.now = 15
        harness.reader.step()
        assert harness.display.screens[-1][0] == ("scanning" if state == "checking" else state)


@pytest.mark.parametrize("mode,expected", [("frame", 2), ("expr", 0)])
def test_display_heartbeat_runs_every_two_seconds(module: Any, tmp_path: Path, mode: str, expected: int) -> None:
    harness = Harness(module, tmp_path, mode)
    harness.reader.start()
    try:
        for now in [1.99, 2, 3.99, 4]:
            harness.clock.now = now
            harness.reader.step()
        assert harness.display.events.count("ping") == expected
    finally:
        harness.reader.close()


def test_disconnect_exits_zero(harness: Harness) -> None:
    client = harness.connect()
    receive(client)
    client.close()
    assert not harness.reader.step()
    assert harness.reader.exit_code == 0


def test_stop_exits_zero_and_restores_display(harness: Harness) -> None:
    harness.reader.stop()
    assert harness.reader.run() == 0
    assert harness.display.events[-1] == "sleeping"
    assert harness.camera.closed


@pytest.mark.parametrize(
    "bad",
    [
        b"not-json secret\n",
        b"\xffsecret\n",
        b"[]\n",
        b'{"type":"screen","state":[]}\n',
        pytest.param(b"x" * 32769 + b"\n", id="oversized"),
    ],
)
def test_invalid_socket_lines_are_discarded_without_leaking_or_losing_next_screen(
    harness: Harness, caplog: pytest.LogCaptureFixture, bad: bytes
) -> None:
    with harness.connect() as client:
        receive(client)
        payload = bad + b'{"type":"screen","state":"checking"}\n'
        for offset in range(0, len(payload), 4096):
            client.sendall(payload[offset : offset + 4096])
            harness.reader.step()
        for _ in range(12):
            harness.reader.step()
        assert harness.display.events[-1] == "checking"
        assert "secret" not in caplog.text


def test_split_socket_line_waits_for_newline(harness: Harness) -> None:
    with harness.connect() as client:
        receive(client)
        client.sendall(b'{"type":"screen",')
        harness.reader.step()
        assert harness.display.events[-1] == "scanning"
        client.sendall(b'"state":"checking"}\n')
        harness.reader.step()
        assert harness.display.events[-1] == "checking"


def test_qr_exceptions_do_not_expose_payload(harness: Harness, caplog: pytest.LogCaptureFixture) -> None:
    class BrokenDecoder:
        def decode(self, frame: Any) -> list[str]:
            raise ValueError("WIFI:P:secret;;")

    harness.reader.decoder = BrokenDecoder()
    with harness.connect() as client:
        receive(client)
        harness.camera.frames.append([])
        assert harness.reader.run() == 1
    assert "secret" not in caplog.text
    assert "ValueError" not in caplog.text


def test_oversized_qr_is_not_sent(harness: Harness) -> None:
    with harness.connect() as client:
        receive(client)
        harness.camera.frames.append(["x" * 32768])
        harness.reader.step()
        assert harness.display.events[-1] == "checking"
        with pytest.raises(TimeoutError):
            client.recv(1)


def test_reservations_publish_shared_metadata_and_release_same_inode(module: Any, tmp_path: Path) -> None:
    with module.reserve_resource("camera", tmp_path):
        path = tmp_path / "camera.lock"
        inode = path.stat().st_ino
        metadata = json.loads(path.read_text())
        assert metadata["pid"] == os.getpid()
        assert metadata["user"]
        assert metadata["app"] == "palmimo-wifi-qr"
        assert metadata["acquired_at"]
        assert path.stat().st_mode & 0o777 == 0o666
        with pytest.raises(module.ReservationUnavailableError) as busy, module.reserve_resource("camera", tmp_path):
            pytest.fail("A second owner acquired the camera")
        assert busy.value.holder["pid"] == os.getpid()
    with module.reserve_resource("camera", tmp_path):
        assert path.stat().st_ino == inode


@pytest.mark.parametrize("kind", ["symlink", "directory", "fifo"])
def test_reservation_rejects_unsafe_lock_paths(module: Any, tmp_path: Path, kind: str) -> None:
    path = tmp_path / "camera.lock"
    target = tmp_path / "target"
    target.write_text("unchanged")
    if kind == "symlink":
        path.symlink_to(target)
    elif kind == "directory":
        path.mkdir()
    else:
        os.mkfifo(path)
    with pytest.raises(module.ReservationUnavailableError), module.reserve_resource("camera", tmp_path):
        pytest.fail("An unsafe lock was acquired")
    assert target.read_text() == "unchanged"


@pytest.mark.parametrize("missing", ["zbar", "serial", "turbojpeg", "shared_library"])
def test_dependency_loader_classifies_missing_runtime_libraries(
    module: Any, monkeypatch: pytest.MonkeyPatch, missing: str
) -> None:
    def import_module(name: str) -> Any:
        if name == missing:
            raise ImportError("unavailable")
        return object()

    def load_library(name: str) -> Any:
        if missing == "shared_library":
            raise OSError("unavailable")
        return object()

    monkeypatch.setattr(module.importlib, "import_module", import_module)
    monkeypatch.setattr(module.ctypes.util, "find_library", lambda name: None if missing == "turbojpeg" else name)
    monkeypatch.setattr(module.ctypes, "CDLL", load_library)
    with pytest.raises(module.DependencyUnavailableError):
        module.load_dependencies()


@pytest.mark.skipif(not hasattr(socket, "SO_PEERCRED"), reason="Linux peer credentials")
def test_peer_credentials_report_kernel_uid(module: Any) -> None:
    first, second = socket.socketpair()
    with first, second:
        assert module.peer_uid(first) == os.getuid()
        assert module.peer_uid(second) == os.getuid()


def test_checking_deadline_refreshes_on_new_screen(harness: Harness) -> None:
    with harness.connect() as client:
        receive(client)
        harness.message(client, state="checking")
        harness.clock.now = 14
        harness.message(client, state="checking")
        harness.clock.now = 15
        harness.reader.step()
        assert harness.display.screens[-1][0] == "checking"
        harness.clock.now = 29
        harness.reader.step()
        assert harness.display.screens[-1][0] == "scanning"


def test_scanning_live_view_stops_on_portal_checking(harness: Harness) -> None:
    with harness.connect() as client:
        receive(client)
        harness.camera.frames.append([])
        harness.reader.step()
        assert harness.display.events[-1] == ("live", [])
        harness.message(client, state="checking")
        harness.camera.frames.append(["secret"])
        harness.reader.step()
        assert harness.display.events[-1] == "checking"
        with pytest.raises(TimeoutError):
            client.recv(1)


def test_isolated_entrypoint_handles_sigterm_and_cleans_up(tmp_path: Path) -> None:
    import signal
    import subprocess
    import time

    script = """
import os, runpy, sys
namespace = runpy.run_path(sys.argv[1])
reader_class = namespace["Reader"]
main = namespace["main"]
class Camera:
    def read(self): return None
    def close(self): print("camera closed", flush=True)
class Display:
    mode = "expr"
    def screen(self, state, fields): pass
    def close(self): print("sleeping", flush=True)
def factory(runtime, uid):
    return reader_class(runtime, uid, reserve=lambda resource: __import__("contextlib").nullcontext(),
        load_dependencies=lambda: None, camera_factory=Camera, display_factory=Display,
        peer_uid_of=lambda connection: os.getuid())
main.__globals__["Reader"] = factory
result = main()
assert not any(name == "palmimo_portal" or name.startswith("palmimo_portal.") for name in sys.modules)
raise SystemExit(result)
"""
    import pwd

    environment = {
        **os.environ,
        "RUNTIME_DIRECTORY": str(tmp_path),
        "PALMIMO_WIFI_QR_PEER_USER": pwd.getpwuid(os.getuid()).pw_name,
    }
    process = subprocess.Popen(
        [sys.executable, "-I", "-c", script, str(READER_PATH)],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 5
        path = tmp_path / "control.sock"
        while not path.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.01)
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(path))
            assert receive(client) == [{"type": "hello", "proto": 1, "display": "expr"}]
            process.send_signal(signal.SIGTERM)
            output, errors = process.communicate(timeout=3)
        assert process.returncode == 0, errors
        assert "sleeping" in output
        assert "camera closed" in output
        assert not path.exists()
    finally:
        if process.poll() is None:
            process.kill()
            process.communicate()
