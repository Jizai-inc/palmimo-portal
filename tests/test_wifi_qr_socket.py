import json
import logging
import threading
from pathlib import Path

import pytest

from palmimo_portal.adapters.wifi_qr_socket import WifiQrSocketClient
from palmimo_portal.testing.wifi_qr_reader import FakeWifiQrReader


@pytest.fixture(autouse=True)
def short_socket_path(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)


def test_socket_delivers_messages_and_sends_screens() -> None:
    received: list[dict[str, object]] = []
    ready = threading.Event()
    caller = threading.get_ident()
    threads: list[int] = []

    def message(value: dict[str, object]) -> None:
        received.append(value)
        threads.append(threading.get_ident())
        ready.set()

    with FakeWifiQrReader(Path("reader.sock")) as reader:
        client = WifiQrSocketClient(message, path=reader.path)
        client.connect()
        reader.send({"type": "hello", "proto": 1, "display": "frame"})
        assert ready.wait(2)
        assert received == [{"type": "hello", "proto": 1, "display": "frame"}]
        assert threads[0] != caller
        client.send({"type": "screen", "state": "connecting", "ssid": "home"})
        assert reader.receive() == {"type": "screen", "state": "connecting", "ssid": "home"}
        client.close()


@pytest.mark.parametrize(
    "bad",
    [
        b'{"type":"qr","text":"secret-password" broken}\n',
        b"\xffsecret-password\n",
        b'["secret-password"]\n',
        b'{"type":"unknown","text":"secret-password"}\n',
        b'{"type":"qr","text":17}\n',
        b'{"type":"hello","proto":2,"display":"frame"}\n',
        b'{"type":"hello","proto":1,"display":"unknown"}\n',
        b"x" * 32769 + b"\n",
        b"[" * 2000 + b'"secret-password"' + b"]" * 2000 + b"\n",
    ],
)
def test_socket_discards_invalid_lines_and_recovers(
    bad: bytes,
    caplog: pytest.LogCaptureFixture,
) -> None:
    received: list[dict[str, object]] = []
    ready = threading.Event()

    def message(value: dict[str, object]) -> None:
        received.append(value)
        ready.set()

    with caplog.at_level(logging.DEBUG), FakeWifiQrReader(Path("reader.sock")) as reader:
        client = WifiQrSocketClient(message, path=reader.path)
        client.connect()
        reader.send_raw(bad)
        reader.send({"type": "qr", "text": "WIFI:S:home;P:secret-password;;"})
        assert ready.wait(2)
        client.close()
    assert received == [{"type": "qr", "text": "WIFI:S:home;P:secret-password;;"}]
    assert "secret-password" not in caplog.text
    assert "WIFI:" not in caplog.text


def test_socket_sanitizes_callback_failure_and_keeps_receiving(
    caplog: pytest.LogCaptureFixture,
) -> None:
    ready = threading.Event()

    def message(value: dict[str, object]) -> None:
        if value["type"] == "qr":
            raise ValueError(str(value))
        ready.set()

    with caplog.at_level(logging.DEBUG), FakeWifiQrReader(Path("reader.sock")) as reader:
        client = WifiQrSocketClient(message, path=reader.path)
        client.connect()
        reader.send({"type": "qr", "text": "WIFI:S:home;P:secret-password;;"})
        reader.send({"type": "hello", "proto": 1, "display": "expr"})
        assert ready.wait(2)
        client.close()
    assert "secret-password" not in caplog.text
    assert "WIFI:" not in caplog.text


def test_socket_reports_disconnect_and_discards_partial_line() -> None:
    disconnected = threading.Event()
    received: list[dict[str, object]] = []
    with FakeWifiQrReader(Path("reader.sock")) as reader:
        client = WifiQrSocketClient(received.append, on_disconnect=disconnected.set, path=reader.path)
        client.connect()
        reader.send_raw(b'{"type":"qr","text":"partial')
        reader.disconnect()
        assert disconnected.wait(2)
        client.close()
    assert received == []
    with pytest.raises(ConnectionError):
        client.send({"type": "screen", "state": "scanning"})


def test_socket_handles_fragmented_and_oversized_lines() -> None:
    ready = threading.Event()
    with FakeWifiQrReader(Path("reader.sock")) as reader:
        client = WifiQrSocketClient(lambda value: ready.set(), path=reader.path)
        client.connect()
        reader.send_raw(b"x" * 100000)
        reader.send_raw(b"\n" + json.dumps({"type": "qr", "text": "fragment"}).encode()[:9])
        reader.send_raw(json.dumps({"type": "qr", "text": "fragment"}).encode()[9:] + b"\n")
        assert ready.wait(2)
        client.close()


def test_socket_drops_oversized_outgoing_line() -> None:
    with FakeWifiQrReader(Path("reader.sock")) as reader:
        client = WifiQrSocketClient(lambda value: None, path=reader.path)
        client.connect()
        assert not client.send({"type": "screen", "ssid": "x" * 32768})
        assert client.send({"type": "screen", "state": "scanning"})
        assert reader.receive() == {"type": "screen", "state": "scanning"}
        client.close()


def test_socket_accepts_line_at_byte_limit() -> None:
    ready = threading.Event()
    received: list[dict[str, object]] = []

    def message(value: dict[str, object]) -> None:
        received.append(value)
        ready.set()

    prefix = b'{"type":"qr","text":"'
    line = prefix + b"x" * (32768 - len(prefix) - 2) + b'"}\n'
    with FakeWifiQrReader(Path("reader.sock")) as reader:
        client = WifiQrSocketClient(message, path=reader.path)
        client.connect()
        reader.send_raw(line)
        assert ready.wait(2)
        client.close()
    assert received == [{"type": "qr", "text": "x" * (32768 - len(prefix) - 2)}]


def test_socket_can_close_from_message_callback() -> None:
    disconnected = threading.Event()
    with FakeWifiQrReader(Path("reader.sock")) as reader:
        client = WifiQrSocketClient(
            lambda value: client.close(),
            on_disconnect=disconnected.set,
            path=reader.path,
        )
        client.connect()
        reader.send({"type": "qr", "text": "secret-password"})
        assert disconnected.wait(2)
        client.close()


def test_socket_sanitizes_outgoing_encoding_error() -> None:
    client = WifiQrSocketClient(lambda value: None)
    with pytest.raises(ValueError) as error:
        client.send({"type": "screen", "ssid": "secret-password\ud800"})
    assert "secret-password" not in str(error.value)
    assert error.value.__suppress_context__
