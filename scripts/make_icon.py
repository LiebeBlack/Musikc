"""Render ``resources/icons/truehq.ico`` from the vector brand mark.

Multi-resolution ICO with PNG-compressed frames (Windows Vista and later), so
the installer and the taskbar each pick the right size without blurring.

Usage:  python scripts/make_icon.py [output.ico]
"""

from __future__ import annotations

import os
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QGuiApplication

SIZES = (16, 24, 32, 48, 64, 128, 256)


def png_bytes(pixmap) -> bytes:
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    pixmap.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


def build_ico(output: Path) -> Path:
    # QPixmap requires a QGuiApplication instance; keep a reference alive.
    _application = QGuiApplication.instance() or QGuiApplication(sys.argv[:1])  # noqa: F841
    from src.ui.main_window import build_app_icon

    icon = build_app_icon(256)
    frames = []
    for size in SIZES:
        pixmap = icon.pixmap(size, size)
        frames.append((size, png_bytes(pixmap)))

    header = struct.pack("<HHH", 0, 1, len(frames))
    entries = bytearray()
    payload = bytearray()
    offset = 6 + 16 * len(frames)
    for size, data in frames:
        dimension = 0 if size >= 256 else size
        entries += struct.pack(
            "<BBBBHHII",
            dimension,
            dimension,
            0,
            0,
            1,
            32,
            len(data),
            offset,
        )
        payload += data
        offset += len(data)

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(header + bytes(entries) + bytes(payload))
    print(f"wrote {output} ({len(frames)} frames, {output.stat().st_size} bytes)")
    return output


def main() -> int:
    output = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "resources" / "icons" / "truehq.ico"
    build_ico(output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
