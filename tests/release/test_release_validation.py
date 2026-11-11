"""A successful process exit must not reuse a report from an earlier run."""

import contextlib
import io
import json
import runpy
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from magic_converter import __version__
from tools.validation import validate_review_epubs

ROOT = Path(__file__).resolve().parents[2]


class FreshReportTests(unittest.TestCase):
    def test_epubcheck_zero_exit_without_new_report_rejects_old_success(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            for name in ("simple_epub", "fb2_roundtrip", "bmp_image", "markdown_table"):
                (folder / (name + "-epubcheck.json")).write_text(
                    json.dumps(
                        {"checker": {"checkerVersion": "5.4.0"}, "messages": []}
                    ),
                    encoding="utf-8",
                )
            with patch.object(
                sys,
                "argv",
                [
                    "validate_review_epubs",
                    "--jar",
                    str(folder / "checker.jar"),
                    "--output",
                    str(folder),
                ],
            ), patch(
                "tools.validation.validate_review_epubs.subprocess.run",
                return_value=subprocess.CompletedProcess([], 0, b"", b""),
            ) as checker, contextlib.redirect_stdout(
                io.StringIO()
            ):
                code = validate_review_epubs.main()
            self.assertEqual(checker.call_count, 4)
            self.assertEqual(code, 1)
            records = json.loads((folder / "summary.json").read_text(encoding="utf-8"))
            self.assertTrue(all(record["checker"] is None for record in records))

    def test_exe_zero_exit_without_new_report_rejects_old_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            tools = root / "tools/validation"
            tools.mkdir(parents=True)
            script = tools / "verify_exe.py"
            shutil.copy2(ROOT / "tools/validation/verify_exe.py", script)
            executable = root / "fake.exe"
            executable.write_bytes(b"Not executed; subprocess is fault-injected")
            destination = root / ".test-artifacts/단독 실행 검증"
            destination.mkdir(parents=True)
            (destination / "exe-self-test.json").write_text(
                json.dumps(
                    {
                        "status": "passed",
                        "version": __version__,
                        "frozen": True,
                        "documents": {
                            "portable": {
                                suffix: {"losses": []}
                                for suffix in (".hwp", ".hwpx", ".docx", ".pdf")
                            },
                        },
                    }
                ),
                encoding="utf-8",
            )
            original_path = list(sys.path)
            try:
                with patch.object(sys, "argv", [str(script), str(executable)]), patch(
                    "subprocess.run", return_value=subprocess.CompletedProcess([], 0)
                ), contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaisesRegex(
                        SystemExit, "did not create its self-test report"
                    ):
                        runpy.run_path(str(script), run_name="__main__")
            finally:
                sys.path[:] = original_path


if __name__ == "__main__":
    unittest.main()
