"""Generate minimal placeholder icons for the Tauri bundle."""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1] / "src-tauri" / "icons"


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    crc = zlib.crc32(tag + data) & 0xFFFFFFFF
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", crc)


def write_png(path: Path, size: int, rgba: tuple[int, int, int, int]) -> None:
    r, g, b, a = rgba
    row = bytes([r, g, b, a]) * size
    raw = b"".join([b"\x00" + row for _ in range(size)])
    compressed = zlib.compress(raw, 9)
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    png = b"\x89PNG\r\n\x1a\n"
    png += _png_chunk(b"IHDR", ihdr)
    png += _png_chunk(b"IDAT", compressed)
    png += _png_chunk(b"IEND", b"")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(png)


def main() -> None:
    color = (168, 44, 28, 255)  # vermillion accent
    write_png(ROOT / "32x32.png", 32, color)
    write_png(ROOT / "128x128.png", 128, color)
    write_png(ROOT / "128x128@2x.png", 256, color)
    # Tauri accepts png as ico/icns placeholders for dev builds.
    write_png(ROOT / "icon.ico", 256, color)
    write_png(ROOT / "icon.icns", 512, color)
    print(f"wrote icons under {ROOT}")


if __name__ == "__main__":
    main()
