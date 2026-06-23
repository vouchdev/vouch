#!/usr/bin/env python3
"""Generate hicolor PNG icons from the scalable SVG (#211).

Uses only the Python standard library (zlib + struct). Run from repo root:

    python desktop/flatpak/scripts/generate-icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

FLATPAK_DIR = Path(__file__).resolve().parents[1]
SVG_PATH = FLATPAK_DIR / "share/icons/hicolor/scalable/apps/com.vouchdev.vouch.svg"
APP_ID = "com.vouchdev.vouch"
SIZES = (16, 32, 48, 64, 128, 256, 512)

# Brand palette from docs/banner.svg
COLOR_BG_TOP = (0xF1, 0xF3, 0xF7)
COLOR_BG_BOTTOM = (0xE2, 0xE6, 0xF3)
COLOR_BAR_TOP = (0x4F, 0x5D, 0x9E)
COLOR_BAR_BOTTOM = (0x1E, 0x2A, 0x44)
COLOR_TEXT = (0x1E, 0x2A, 0x44)
COLOR_ACCENT = (0x4F, 0x5D, 0x9E)


def _lerp(a: int, b: int, t: float) -> int:
    return int(a + (b - a) * t)


def _lerp_rgb(
    c1: tuple[int, int, int], c2: tuple[int, int, int], t: float
) -> tuple[int, int, int]:
    return (_lerp(c1[0], c2[0], t), _lerp(c1[1], c2[1], t), _lerp(c1[2], c2[2], t))


def _render_icon(size: int) -> list[tuple[int, int, int]]:
    """Rasterize a simplified vector motif matching the SVG."""
    pixels: list[tuple[int, int, int]] = []
    radius = size * 96 / 512
    bar_x = size * 108 / 512
    bar_w = size * 56 / 512
    bar_y = size * 96 / 512
    bar_h = size * 320 / 512
    circle_cx = size * 392 / 512
    circle_cy = size * 360 / 512
    circle_r = size * 28 / 512

    for y in range(size):
        for x in range(size):
            t = (x + y) / (2 * (size - 1)) if size > 1 else 0.0
            bg = _lerp_rgb(COLOR_BG_TOP, COLOR_BG_BOTTOM, t)

            # rounded rect background clip (approx)
            dx = min(x, size - 1 - x)
            dy = min(y, size - 1 - y)
            corner = radius
            outside = (dx < corner and dy < corner) and (
                (corner - dx) ** 2 + (corner - dy) ** 2 > corner**2
            )
            if outside:
                pixels.append((0, 0, 0))
                continue

            color = bg

            # vertical bar with gradient
            if bar_x <= x <= bar_x + bar_w and bar_y <= y <= bar_y + bar_h:
                bar_t = (y - bar_y) / bar_h if bar_h else 0
                color = _lerp_rgb(COLOR_BAR_TOP, COLOR_BAR_BOTTOM, bar_t)

            # 'v' stem (simplified block letter)
            text_x0 = size * 200 / 512
            text_x1 = size * 310 / 512
            text_y0 = size * 170 / 512
            text_y1 = size * 310 / 512
            if text_x0 <= x <= text_x1 and text_y0 <= y <= text_y1:
                color = COLOR_TEXT

            # review circle accent
            dist = ((x - circle_cx) ** 2 + (y - circle_cy) ** 2) ** 0.5
            if circle_r - size * 6 / 512 <= dist <= circle_r:
                color = COLOR_ACCENT

            pixels.append(color)
    return pixels


def _write_png(path: Path, size: int, pixels: list[tuple[int, int, int]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = bytearray()
    for y in range(size):
        raw.append(0)  # filter type None
        start = y * size
        for x in range(size):
            r, g, b = pixels[start + x]
            raw.extend((r, g, b))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", ihdr)
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    if not SVG_PATH.is_file():
        raise SystemExit(f"scalable icon missing: {SVG_PATH}")

    for size in SIZES:
        pixels = _render_icon(size)
        out = (
            FLATPAK_DIR
            / "share/icons/hicolor"
            / f"{size}x{size}"
            / "apps"
            / f"{APP_ID}.png"
        )
        _write_png(out, size, pixels)
        print(f"wrote {out} ({out.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
