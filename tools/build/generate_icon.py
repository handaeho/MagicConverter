"""Validate the checked-in icon or render it from the project's SVG."""

import argparse
from pathlib import Path
from PIL import Image

root = Path(__file__).resolve().parents[2]
destination = root / "assets" / "magic_converter-tool.ico"
sizes = (16, 20, 24, 32, 40, 48, 64, 96, 128, 256)


def render_icon():
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    source = destination.with_suffix(".svg")
    renderer = QSvgRenderer(str(source))
    if not renderer.isValid():
        raise SystemExit(f"Invalid source SVG: {source}")
    frames = []
    for size in sizes:
        canvas = QImage(size, size, QImage.Format.Format_RGBA8888)
        canvas.fill(Qt.GlobalColor.transparent)
        painter = QPainter(canvas)
        try:
            painter.setRenderHint(QPainter.RenderHint.Antialiasing)
            renderer.render(painter, QRectF(0, 0, size, size))
        finally:
            painter.end()
        frames.append(Image.frombytes("RGBA", (size, size), bytes(canvas.constBits())))
    frames[-1].save(destination, format="ICO", sizes=[frame.size for frame in frames], append_images=frames[:-1])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--render", action="store_true")
    args = parser.parse_args()
    if args.render:
        render_icon()
    if not destination.is_file():
        raise SystemExit(f"Required icon is missing: {destination}")
    with Image.open(destination) as artwork:
        if artwork.format != "ICO" or artwork.ico.sizes() != {(size, size) for size in sizes}:
            raise SystemExit(f"Expected a Windows ICO with the required sizes: {destination}")
        for size in sizes:
            artwork.ico.getimage((size, size)).load()
    print(destination)


if __name__ == "__main__":
    main()
