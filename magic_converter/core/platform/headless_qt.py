"""Configure Qt renderers that run in Windows worker processes."""

import os
from pathlib import Path


def configure_headless_qt() -> None:
    platform = os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if os.name == "nt" and platform.split(":", 1)[0] == "offscreen":
        # Qt's basic offscreen font database does not discover Windows fonts.
        fonts = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts"
        if fonts.is_dir():
            os.environ.setdefault("QT_QPA_FONTDIR", str(fonts))
