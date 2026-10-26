"""Build reader assets: uv run --with pillow==13.0.0 python tools/render_wifi_qr_screens.py FONT.otf."""

import argparse
import runpy
import struct
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont


ROOT = Path(__file__).resolve().parents[1]
DESTINATION = ROOT / "palmimo_portal/wifi_qr_reader"
REASONS = {
    "not_wifi": ("Show a Wi-Fi QR code", "to set up your network."),
    "unsupported_security": ("This Wi-Fi type", "is not supported.", "Choose another network."),
    "hidden_network": ("Hidden Wi-Fi is not", "supported here.", "Use setup in your browser."),
    "invalid_ssid": ("Check the network name.", "Create a new Wi-Fi QR."),
    "invalid_psk": ("Check the password.", "Create a new Wi-Fi QR."),
    "busy": ("Another connection", "is being checked.", "Please wait."),
    "connect_failed": ("Check the password", "and signal, then try again."),
}


def render(font_path: Path) -> None:
    namespace = runpy.run_path(str(DESTINATION / "reader.py"))
    title_font = ImageFont.truetype(str(font_path), 16)
    body_font = ImageFont.truetype(str(font_path), 11)
    screens = {
        "checking": ("Checking QR", ("Please wait...",), "#ffc857"),
        "connecting": ("Connecting", ("Please wait...",), "#ffc857"),
        "connected": ("Connected", (), "#70e0ae"),
    }
    for state in ("rejected", "failed"):
        for reason, reason_lines in REASONS.items():
            screens[f"{state}-{reason}"] = (
                "Check this QR" if state == "rejected" else "Could not connect",
                reason_lines,
                "#ff9b8c",
            )
    for name, (title, lines, accent) in screens.items():
        image = Image.new("RGB", (168, 168), "#101b29")
        draw = ImageDraw.Draw(image)
        if name == "connected":
            draw.text((84, 11), title, font=title_font, fill=accent, anchor="mm")
            x0, y0, x1, y1 = namespace["QR_RECT"]
            draw.rectangle((x0, y0, x1 - 1, y1 - 1), fill="white")
            x0, y0, x1, y1 = namespace["CLOSE_RECT"]
            draw.rounded_rectangle((x0, y0, x1 - 1, y1 - 1), radius=4, fill=accent)
            draw.text(((x0 + x1) / 2, (y0 + y1) / 2 - 1), "Close", font=body_font, fill="#101b29", anchor="mm")
            small = ImageFont.truetype(str(font_path), 8)
            draw.text((84, 159), "Closes automatically in 5 minutes", font=small, fill="white", anchor="mm")
        else:
            draw.rounded_rectangle((7, 7, 160, 160), radius=12, outline=accent, width=2)
            draw.text((84, 32), title, font=title_font, fill=accent, anchor="mm")
            draw.line((22, 55, 145, 55), fill=accent, width=2)
            top = 124 if name == "connecting" else 81
            for index, line in enumerate(lines):
                draw.text((84, top + index * 17), line, font=body_font, fill="white", anchor="mm")
        pixels = bytearray()
        rgb = image.tobytes()
        for offset in range(0, len(rgb), 3):
            red, green, blue = rgb[offset : offset + 3]
            pixels.extend(struct.pack("<H", (red >> 3) << 11 | (green >> 2) << 5 | blue >> 3))
        (DESTINATION / f"{name}.bin").write_bytes(pixels)
    width, height = namespace["FONT_WIDTH"], namespace["FONT_HEIGHT"]
    font = ImageFont.truetype(str(font_path), 8)
    glyphs = {}
    for codepoint in range(32, 127):
        image = Image.new("1", (width, height))
        draw = ImageDraw.Draw(image)
        draw.text((width / 2, height - 2), chr(codepoint), font=font, fill=1, anchor="ms")
        glyphs[chr(codepoint)] = tuple(
            sum(0x80 >> x for x in range(width) if image.getpixel((x, y))) for y in range(height)
        )
    glyphs["…"] = (0,) * 7 + (0xA8, 0, 0)
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
