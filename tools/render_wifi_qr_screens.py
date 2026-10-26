"""Build reader assets: uv run --with pillow==13.0.0 python tools/render_wifi_qr_screens.py FONT.otf."""

import argparse
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "palmimo_portal/wifi_qr_reader"
REASONS = {
    "not_wifi": ("Wi-Fi 用の QR を", "見せてください"),
    "unsupported_security": ("この方式は使えません", "別の Wi-Fi を選んで"),
    "hidden_network": ("非公開の Wi-Fi です", "Web から設定してね"),
    "invalid_ssid": ("Wi-Fi 名を確認して", "QR を作り直してね"),
    "invalid_psk": ("パスワードを確認して", "QR を作り直してね"),
    "busy": ("別の接続を確認中", "少し待ってください"),
    "connect_failed": ("パスワードと電波を", "確かめて再度お試しを"),
}


def render(font_path: Path) -> None:
    title_font = ImageFont.truetype(str(font_path), 18)
    body_font = ImageFont.truetype(str(font_path), 13)
    screens = {
        "checking": ("確認中", ("少し待ってください",), "#ffc857"),
        "connecting": ("接続中", ("少し待ってください",), "#ffc857"),
        "connected": ("接続できました", ("上のアドレスを開いて", "本体のシールの", "パスワードでログイン"), "#70e0ae"),
    }
    for state in ("rejected", "failed"):
        for reason, reason_lines in REASONS.items():
            screens[f"{state}-{reason}"] = (
                "QR を確認して" if state == "rejected" else "接続できません",
                reason_lines,
                "#ff9b8c",
            )
    for name, (title, lines, accent) in screens.items():
        image = Image.new("RGB", (168, 168), "#101b29")
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle((9, 9, 158, 158), radius=14, outline=accent, width=2)
        draw.line((24, 57, 143, 57), fill=accent, width=2)
        draw.text((84, 32), title, font=title_font, fill=accent, anchor="mm")
        top = 115 if name == "connected" else 126 if name == "connecting" else 81
        for index, line in enumerate(lines):
            draw.text((84, top + index * 14), line, font=body_font, fill="white", anchor="mm")
        pixels = bytearray()
        rgb = image.tobytes()
        for offset in range(0, len(rgb), 3):
            red, green, blue = rgb[offset : offset + 3]
            pixels.extend(struct.pack("<H", (red >> 3) << 11 | (green >> 2) << 5 | blue >> 3))
        (DESTINATION / f"{name}.bin").write_bytes(pixels)
    font = ImageFont.truetype(str(font_path), 10)
    glyphs = {}
    for codepoint in range(32, 127):
        image = Image.new("1", (8, 12))
        draw = ImageDraw.Draw(image)
        draw.text((4, 10), chr(codepoint), font=font, fill=1, anchor="ms")
        glyphs[chr(codepoint)] = tuple(sum(0x80 >> x for x in range(8) if image.getpixel((x, y))) for y in range(12))
    source = DESTINATION / "reader.py"
    text = source.read_text()
    start = text.index("ASCII_FONT:")
    end = text.index("\n\n\ndef draw_ascii", start)
    table = "ASCII_FONT: dict[str, tuple[int, ...]] = {\n"
    for character, rows in glyphs.items():
        table += f"    {character!r}: {rows!r},\n"
    table += "}"
    source.write_text(text[:start] + table + text[end:])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("font", type=Path)
    render(parser.parse_args().font)
