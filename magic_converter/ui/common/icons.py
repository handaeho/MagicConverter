"""One icon path shared by source runs, the tray and frozen executables."""

import sys
from pathlib import Path

from PySide6.QtGui import QIcon

ICON_FILENAME = "magic_converter-tool.ico"


def icon_path() -> Path:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    return root / "assets" / ICON_FILENAME


def make_icon() -> QIcon:
    path = icon_path()
    icon = QIcon(str(path))
    if icon.isNull():
        raise RuntimeError(f"앱 아이콘을 읽을 수 없습니다: {path}")
    return icon
