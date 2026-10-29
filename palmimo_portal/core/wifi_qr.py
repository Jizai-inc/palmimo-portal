from dataclasses import dataclass, field
from string import hexdigits
from typing import TypedDict

import segno


class QrMatrix(TypedDict):
    size: int
    rows: list[str]


def make_url_qr(url: str) -> QrMatrix:
    qr = segno.make_qr(url, error="M", boost_error=False)
    return {"size": len(qr.matrix), "rows": ["".join(str(bit) for bit in row) for row in qr.matrix]}


@dataclass(frozen=True)
class WifiCredential:
    ssid: str
    password: str = field(repr=False)


@dataclass(frozen=True)
class Rejection:
    reason: str


def parse_wifi_qr(text: str) -> WifiCredential | Rejection:
    if not text.startswith("WIFI:"):
        return Rejection("not_wifi")
    fields: dict[str, str] = {}
    chunk: list[str] = []
    escaped = False
    for char in text[5:] + ";":
        if escaped:
            chunk.append(char)
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == ";":
            key, separator, value = "".join(chunk).partition(":")
            if separator:
                fields[key] = value
            chunk.clear()
        else:
            chunk.append(char)
    security = fields.get("T", "")
    if security not in {"", "WPA", "WPA2", "nopass"}:
        return Rejection("unsupported_security")
    if fields.get("H") == "true":
        return Rejection("hidden_network")
    ssid = fields.get("S", "")
    try:
        ssid_size = len(ssid.encode("utf-8"))
    except UnicodeError:
        return Rejection("invalid_ssid")
    if not 1 <= ssid_size <= 32:
        return Rejection("invalid_ssid")
    password = fields.get("P", "") if security != "nopass" else ""
    if security != "nopass" and not (
        8 <= len(password) <= 63 or (len(password) == 64 and all(char in hexdigits for char in password))
    ):
        return Rejection("invalid_psk")
    return WifiCredential(ssid, password)
