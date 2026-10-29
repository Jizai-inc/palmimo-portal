import os
import queue
import socket
import threading
from contextlib import nullcontext
from pathlib import Path
from typing import Any

from palmimo_portal.adapters.wifi_qr_socket import WifiQrSocketClient
from palmimo_portal.core.wifi_qr import make_url_qr
from tests import test_wifi_qr_reader as reader_tests


module = reader_tests.module
tmp_path = reader_tests.tmp_path


def test_portal_socket_client_exchanges_setup_messages_with_real_reader(module: Any, tmp_path: Path) -> None:
    frames: queue.Queue[list[str]] = queue.Queue()
    screens: queue.Queue[tuple[str, dict[str, Any]]] = queue.Queue()
    received: queue.Queue[dict[str, Any]] = queue.Queue()
    completed: queue.Queue[int] = queue.Queue()
    camera_closed = threading.Event()
    display_closed = threading.Event()
    close_button = threading.Event()
    disconnected = threading.Event()

    class Camera:
        def read(self) -> list[str] | None:
            try:
                return frames.get_nowait()
            except queue.Empty:
                return None

        def close(self) -> None:
            camera_closed.set()

    class Display:
        def screen(self, state: str, fields: dict[str, Any]) -> None:
            screens.put((state, fields))

        def live(self, frame: Any) -> None:
            pass

        def ping(self) -> None:
            pass

        def take_close(self) -> bool:
            pressed = close_button.is_set()
            close_button.clear()
            return pressed

        def close(self) -> None:
            display_closed.set()

    reader = module.Reader(
        tmp_path,
        os.getuid(),
        reserve=lambda resource: nullcontext(),
        load_dependencies=lambda: None,
        camera_factory=Camera,
        decoder_factory=reader_tests.Decoder,
        display_factory=Display,
        peer_uid_of=module.peer_uid if hasattr(socket, "SO_PEERCRED") else lambda connection: os.getuid(),
    )
    client = WifiQrSocketClient(received.put, on_disconnect=disconnected.set, path=reader.socket_path)
    worker = threading.Thread(target=lambda: completed.put(reader.run()), name="reader-interop", daemon=True)
    reader.start()
    worker.start()
    try:
        client.connect()
        assert received.get(timeout=2) == {"type": "hello", "proto": 1}
        assert screens.get(timeout=2) == ("scanning", {})
        assert client.send({"type": "screen", "state": "scanning"})
        assert screens.get(timeout=2) == ("scanning", {})

        text = "WIFI:T:WPA;S:home;P:test-password;;"
        frames.put([text])
        assert screens.get(timeout=2) == ("checking", {})
        assert received.get(timeout=2) == {"type": "qr", "text": text}

        url = "http://robot.local"
        qr = make_url_qr(url)
        assert client.send({"type": "screen", "state": "connected", "url": url, "qr": qr})
        assert screens.get(timeout=2) == ("connected", {"url": url, "qr": qr})
        close_button.set()
        assert received.get(timeout=2) == {"type": "close"}

        client.close()
        worker.join(timeout=2)
        assert not worker.is_alive()
        assert completed.get(timeout=2) == 0
        assert disconnected.wait(2)
        assert camera_closed.is_set()
        assert display_closed.is_set()
        assert not reader.socket_path.exists()
    finally:
        client.close()
        reader.stop()
        worker.join(timeout=2)
        if not worker.is_alive():
            reader.close()
