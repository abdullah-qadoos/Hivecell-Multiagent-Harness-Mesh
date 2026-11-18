"""Regenerate hivecell/app/icon.ico: the honeycomb mark, PNG-in-ICO at several sizes.

Windows only (draws with System.Drawing through pythonnet, which pywebview already installs).
Run: uv run python scripts/make_icon.py
"""
import io
import math
import struct
from pathlib import Path

import clr

clr.AddReference("System.Drawing")
from System.Drawing import Bitmap, Color, Graphics, PointF, SolidBrush  # noqa: E402
from System.Drawing.Drawing2D import SmoothingMode  # noqa: E402
from System.Drawing.Imaging import ImageFormat  # noqa: E402
from System.IO import MemoryStream  # noqa: E402

ORANGE = Color.FromArgb(255, 0xD4, 0x60, 0x2A)
CREAM = Color.FromArgb(255, 0xF3, 0xED, 0xE2)
SIZES = [16, 24, 32, 48, 64, 128, 256]
OUT = Path(__file__).resolve().parents[1] / "hivecell" / "app" / "icon.ico"


def hexagon(cx, cy, r):
    return [PointF(cx + r * math.cos(math.radians(90 + 60 * i)), cy - r * math.sin(math.radians(90 + 60 * i)))
            for i in range(6)]


def png(size: int) -> bytes:
    bmp = Bitmap(size, size)
    g = Graphics.FromImage(bmp)
    g.SmoothingMode = SmoothingMode.AntiAlias
    g.Clear(Color.Transparent)
    c = size / 2
    # honeycomb at every size, so the taskbar and title bar match the logo; gaps stay at least 1px when tiny
    gap = max(1.0, size * 0.035)
    r = (size * 0.5 - gap) / (1 + math.sqrt(3))  # outer cells touch the edge, nothing clipped
    d = math.sqrt(3) * r + gap
    g.FillPolygon(SolidBrush(CREAM), hexagon(c, c, r))
    for a in range(0, 360, 60):
        g.FillPolygon(SolidBrush(ORANGE), hexagon(c + d * math.cos(math.radians(a)), c + d * math.sin(math.radians(a)), r))
    ms = MemoryStream()
    bmp.Save(ms, ImageFormat.Png)
    data = bytes(ms.ToArray())
    g.Dispose()
    bmp.Dispose()
    return data


def main():
    images = [png(s) for s in SIZES]
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, data in zip(SIZES, images):
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO format
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset + len(blobs))
        blobs += data
    OUT.write_bytes(header + entries + blobs)
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, sizes {SIZES})")


if __name__ == "__main__":
    main()
