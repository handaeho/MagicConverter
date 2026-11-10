"""Runtime regression for the diagnostic/version import after package moves."""

import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from magic_converter import __version__
from magic_converter.core.diagnostics.diagnostics import run_self_test

APP = QApplication.instance() or QApplication([])


class PackageLayoutTests(unittest.TestCase):
    def test_self_test_reaches_engine_check_and_writes_current_version(self):
        with tempfile.TemporaryDirectory() as directory:
            report = Path(directory) / "self-test.json"
            with patch(
                "magic_converter.core.conversion.converter.find_ffmpeg", return_value=None
            ):
                result = run_self_test(report)

            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(result, 1)
            self.assertEqual(data["status"], "failed")
            self.assertEqual(data["version"], __version__)
            self.assertEqual(data["icon"], "magic_converter-tool.ico")
            self.assertIn("FFmpeg not found", data["error"])
            self.assertNotIn("ImportError", data["error"])


if __name__ == "__main__":
    unittest.main()
