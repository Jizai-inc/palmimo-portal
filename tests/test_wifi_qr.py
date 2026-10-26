import logging

import pytest

from palmimo_portal.core.wifi_qr import Rejection, WifiCredential, parse_wifi_qr


@pytest.mark.parametrize(
    "text",
    [
        r'WIFI:T:WPA;S:QR\;テスト\:”1”;P:p@ss\;w,rd\:"x"\\ 100%;;',
        r'WIFI:S:QR\;テスト\:”1”;T:WPA;P:p@ss\;w\,rd\:"x"\\ 100%;H:false;;',
    ],
)
def test_parse_wifi_qr_accepts_phone_samples(text: str) -> None:
    assert parse_wifi_qr(text) == WifiCredential("QR;テスト:”1”", 'p@ss;w,rd:"x"\\ 100%')


@pytest.mark.parametrize("security", ["WPA", "WPA2", ""])
def test_parse_wifi_qr_accepts_supported_security(security: str) -> None:
    field = f"T:{security};" if security else ""
    assert parse_wifi_qr(f"WIFI:{field}S:home;P:password;;") == WifiCredential("home", "password")


@pytest.mark.parametrize(
    "text,expected",
    [
        ("https://example.com", "not_wifi"),
        *[(f"WIFI:T:{t};S:home;P:password;;", "unsupported_security") for t in ["SAE", "WPA3", "WEP", "WPA2-EAP"]],
        ("WIFI:S:home;P:password;H:true;;", "hidden_network"),
        ("WIFI:S:;P:password;;", "invalid_ssid"),
        ("WIFI:P:password;;", "invalid_ssid"),
        ("WIFI:S:" + "a" * 33 + ";P:password;;", "invalid_ssid"),
        ("WIFI:S:" + "界" * 11 + ";P:password;;", "invalid_ssid"),
        ("WIFI:S:\ud800;P:password;;", "invalid_ssid"),
        *[(f"WIFI:S:home;P:{p};;", "invalid_psk") for p in ["", "short", "z" * 64, "a" * 65]],
    ],
)
def test_parse_wifi_qr_rejects_invalid_credentials(text: str, expected: str, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.DEBUG):
        assert parse_wifi_qr(text) == Rejection(expected)
    assert text not in caplog.text
    assert "password" not in caplog.text


@pytest.mark.parametrize("password", ["a" * 8, "界" * 63, "abcdef0123456789" * 4])
def test_parse_wifi_qr_accepts_psk_boundaries(password: str) -> None:
    assert parse_wifi_qr(f"WIFI:S:{'a' * 32};P:{password};;") == WifiCredential("a" * 32, password)


def test_parse_wifi_qr_accepts_open_network() -> None:
    assert parse_wifi_qr("WIFI:T:nopass;S:home;;") == WifiCredential("home", "")
