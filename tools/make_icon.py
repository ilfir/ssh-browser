"""Draws the Norton-Commander-style app icon and builds assets/AppIcon.icns (macOS only)."""
import os
import shutil
import subprocess
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QGuiApplication, QImage, QPainter, QPen

BLUE, CYAN, YELLOW, WHITE, BLACK, TEAL = "#0000AA", "#55FFFF", "#FFFF55", "#FFFFFF", "#000000", "#00AAAA"


def draw(size: int = 1024) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.TextAntialiasing)
    s = size / 1024

    def R(x, y, w, h): return QRectF(x * s, y * s, w * s, h * s)

    # macOS-style squircle-ish tile
    p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(BLUE))
    p.drawRoundedRect(R(40, 40, 944, 944), 190 * s, 190 * s)

    def frame(r, color=CYAN):
        pen = QPen(QColor(color)); pen.setWidthF(9 * s); p.setPen(pen); p.setBrush(Qt.BrushStyle.NoBrush)
        p.drawRect(R(*r)); p.drawRect(R(r[0] + 26, r[1] + 26, r[2] - 52, r[3] - 52))  # double line

    font = QFont("Menlo"); font.setBold(True); font.setPixelSize(max(1, int(58 * s))); p.setFont(font)

    def text(x, y, t, color):
        p.setPen(QColor(color)); p.drawText(R(x, y, 380, 70), int(Qt.AlignmentFlag.AlignVCenter), t)

    # two panels
    left, right = (110, 150, 390, 600), (524, 150, 390, 600)
    frame(left); frame(right)
    for (x, y, w, h), name in ((left, "REMOTE"), (right, "LOCAL")):
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(BLUE))
        p.drawRect(R(x + w / 2 - 90, y - 24, 180, 50))
        font.setPixelSize(max(1, int(44 * s))); p.setFont(font)
        p.setPen(QColor(BLACK)); p.setBrush(QColor(TEAL))
        p.drawRect(R(x + w / 2 - 90, y - 24, 180, 50)); p.drawText(R(x + w / 2 - 90, y - 24, 180, 50), int(Qt.AlignmentFlag.AlignCenter), name)
    font.setPixelSize(max(1, int(52 * s))); p.setFont(font)
    rows = [("..", CYAN), ("Documents", WHITE), ("Projects", WHITE), ("app.log", CYAN), ("backup.zip", CYAN), ("notes.txt", CYAN)]
    for i, (n, c) in enumerate(rows):
        y = 190 + i * 72
        if i == 2:  # selection bar
            p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(TEAL)); p.drawRect(R(150, y, 310, 60))
            text(166, y - 5, n, BLACK)
        else:
            text(166, y - 5, n, c)
    for i, n in enumerate(["config", "src", "build", "readme"]):
        text(570, 190 + i * 72 - 5, n, YELLOW if i == 0 else CYAN)

    # arrows between panels: the "ssh" link
    p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(YELLOW))
    cx, cy = 512, 640
    p.drawPolygon([QPointF(*(a * s for a in pt))
                   for pt in ((cx - 62, cy - 22), (cx + 4, cy - 22), (cx + 4, cy - 56), (cx + 62, cy), (cx + 4, cy + 56), (cx + 4, cy + 22), (cx - 62, cy + 22))])

    # function-key bar
    keys = ["1", "2", "3", "4", "5", "6", "7"]
    font.setPixelSize(max(1, int(40 * s))); p.setFont(font)
    for i, k in enumerate(keys):
        x = 110 + i * 116
        p.setPen(Qt.PenStyle.NoPen); p.setBrush(QColor(TEAL)); p.drawRect(R(x + 34, 790, 76, 56))
        p.setPen(QColor(WHITE)); p.drawText(R(x, 790, 34, 56), int(Qt.AlignmentFlag.AlignCenter), k)
    font.setPixelSize(max(1, int(108 * s))); p.setFont(font); p.setPen(QColor(YELLOW))
    p.drawText(R(110, 850, 804, 120), int(Qt.AlignmentFlag.AlignCenter), "ssh://")
    p.end()
    return img


def main(out: Path):
    app = QGuiApplication.instance() or QGuiApplication(sys.argv)
    out.parent.mkdir(parents=True, exist_ok=True)
    draw(1024).save(str(out.with_suffix(".png")))
    iconset = out.with_suffix(".iconset")
    shutil.rmtree(iconset, ignore_errors=True); iconset.mkdir()
    for base in (16, 32, 128, 256, 512):
        draw(base).save(str(iconset / f"icon_{base}x{base}.png"))
        draw(base * 2).save(str(iconset / f"icon_{base}x{base}@2x.png"))
    subprocess.run(["iconutil", "-c", "icns", str(iconset), "-o", str(out)], check=True)
    shutil.rmtree(iconset)
    print("wrote", out)


if __name__ == "__main__":
    main(Path(__file__).resolve().parent.parent / "assets" / "AppIcon.icns")
