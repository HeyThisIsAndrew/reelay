"""Draw the app icon and export icon.png, icon.ico (Windows) and icon.icns (macOS).

Run from the project root:  python packaging/make_icons.py
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QLinearGradient, QPainter, QPainterPath, QPen

ASSETS = Path(__file__).resolve().parent / "assets"


def draw(size: int = 1024) -> QImage:
    img = QImage(size, size, QImage.Format_ARGB32)
    img.fill(Qt.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.Antialiasing)
    s = size / 1024

    # macOS icon grid: 824px rounded square centred on a 1024 canvas
    body = QRectF(100 * s, 100 * s, 824 * s, 824 * s)
    grad = QLinearGradient(body.topLeft(), body.bottomLeft())
    grad.setColorAt(0, QColor("#33322f"))
    grad.setColorAt(1, QColor("#1a1918"))
    path = QPainterPath()
    path.addRoundedRect(body, 185 * s, 185 * s)
    p.fillPath(path, grad)
    p.setPen(QPen(QColor(255, 255, 255, 28), 3 * s))
    p.drawPath(path)

    # Down arrow into a tray, in white
    pen = QPen(QColor("#ecebe7"), 64 * s, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin)
    p.setPen(pen)
    cx = 512 * s
    p.drawLine(QPointF(cx, 290 * s), QPointF(cx, 590 * s))
    arrow = QPainterPath(QPointF(cx - 135 * s, 465 * s))
    arrow.lineTo(cx, 600 * s)
    arrow.lineTo(cx + 135 * s, 465 * s)
    p.drawPath(arrow)
    tray = QPainterPath(QPointF(300 * s, 640 * s))
    tray.lineTo(300 * s, 735 * s)
    tray.lineTo(724 * s, 735 * s)
    tray.lineTo(724 * s, 640 * s)
    p.drawPath(tray)
    p.end()
    return img


def main() -> int:
    app = QGuiApplication(sys.argv)  # noqa: F841 — needed for QPainter text/raster setup
    ASSETS.mkdir(parents=True, exist_ok=True)
    big = draw(1024)
    big.save(str(ASSETS / "icon.png"))

    # Windows .ico: Qt writes the multi-resolution file from the 256px image
    big.scaled(256, 256, Qt.KeepAspectRatio, Qt.SmoothTransformation).save(str(ASSETS / "icon.ico"))

    if sys.platform == "darwin" and shutil.which("iconutil"):
        with tempfile.TemporaryDirectory() as tmp:
            iconset = Path(tmp) / "icon.iconset"
            iconset.mkdir()
            for pt in (16, 32, 128, 256, 512):
                for scale in (1, 2):
                    px = pt * scale
                    name = f"icon_{pt}x{pt}{'@2x' if scale == 2 else ''}.png"
                    draw(px).save(str(iconset / name))
            subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(ASSETS / "icon.icns")], check=True)
    print("Icons written to", ASSETS)
    return 0


if __name__ == "__main__":
    sys.exit(main())
