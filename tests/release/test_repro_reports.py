"""A07: each invocation replaces stale success, including early exceptions."""

import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from tools.validation import verify_repro_build as repro


class ReproReportTests(unittest.TestCase):
    def setUp(self):
        preflight = patch.object(repro, 'check_environment', return_value={'status': 'passed'})
        preflight.start()
        self.addCleanup(preflight.stop)

    def test_environment_failure_stops_before_snapshot_and_invalidates_old_success(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            output = root / '.test-artifacts/run'
            output.mkdir(parents=True)
            (output / 'result.json').write_text('{"status":"passed"}', encoding='utf-8')
            with patch.object(repro, 'ROOT', root), patch.object(sys, 'argv', ['repro', '--output', str(output)]), patch.object(
                    repro, 'check_environment', side_effect=RuntimeError('localhost denied')), patch.object(
                    repro, 'reproduce') as build, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(repro.main(), 1)
            build.assert_not_called()
            result = json.loads((output / 'result.json').read_text(encoding='utf-8'))
            self.assertEqual(result['status'], 'failed')
            self.assertIn('localhost denied', result['error'])

    def test_early_failures_replace_old_success_and_identify_the_new_run(self):
        for failure in ("git", "copy", "venv"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                output = root / ".test-artifacts/run"
                output.mkdir(parents=True)
                old = output / "result.json"
                old.write_text('{"status":"passed","run_id":"old"}', encoding="utf-8")
                (root / "ok.py").write_text("pass", encoding="utf-8")
                listing = [b"missing.py\0" if failure == "copy" else b"ok.py\0", b""]
                with patch.object(repro, "ROOT", root), patch.object(sys, "argv", ["repro", "--output", str(output)]), patch.object(
                    repro.subprocess, "check_output", side_effect=RuntimeError("git failed") if failure == "git" else listing
                ), patch.object(repro.subprocess, "run", side_effect=RuntimeError("venv failed")), contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(repro.main(), 1)
                result = json.loads(old.read_text(encoding="utf-8"))
                self.assertEqual(result["status"], "failed")
                self.assertNotEqual(result["run_id"], "old")
                self.assertTrue(result["started_utc"])
                self.assertTrue(result["finished_utc"])
                self.assertIn("error", result)

    def test_success_records_new_identity_and_compares_all_four_artifacts(self):
        from magic_converter import __version__
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp).resolve()
            output = root / ".test-artifacts/run"
            source = root / ".test-artifacts/source"
            source.mkdir(parents=True)
            for base in (root, source):
                release = base / "dist" / ("v" + __version__)
                release.mkdir(parents=True)
                for name in ("magic_converter.exe", "magic_converter-cli.exe", "sbom.spdx.json", "build-inputs.json"):
                    (release / name).write_bytes(b"identical " + name.encode())
            with patch.object(repro, "ROOT", root), patch.object(sys, "argv", ["repro", "--output", str(output), "--work-path", "build/release-v2219"]), patch.object(
                repro.tempfile, "mkdtemp", return_value=str(source)
            ), patch.object(repro.subprocess, "check_output", side_effect=[b"", b""]), patch.object(
                repro.subprocess, "run", return_value=subprocess.CompletedProcess([], 0)
            ) as run, contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(repro.main(), 0)
            build_command = run.call_args_list[1].args[0]
            self.assertEqual(build_command[build_command.index("-WorkPath") + 1], "build/release-v2219")
            result = json.loads((output / "result.json").read_text(encoding="utf-8"))
            self.assertEqual(result["status"], "passed")
            self.assertEqual(len(result["compared"]), 4)
            self.assertEqual(result["source_self_test_exit"], 0)
            self.assertTrue(result["run_id"])

    def test_invalid_work_paths_fail_before_snapshot_or_build(self):
        for work_path in ("../build/release", "build/../release", "dist/release", "C:/outside"):
            with self.subTest(work_path=work_path), tempfile.TemporaryDirectory() as temp:
                root = Path(temp).resolve()
                output = root / ".test-artifacts/run"
                with patch.object(repro, "ROOT", root), patch.object(sys, "argv", [
                    "repro", "--output", str(output), "--work-path", work_path
                ]), patch.object(repro, "reproduce") as build, contextlib.redirect_stdout(io.StringIO()):
                    self.assertEqual(repro.main(), 1)
                build.assert_not_called()
                result = json.loads((output / "result.json").read_text(encoding="utf-8"))
                self.assertEqual(result["status"], "failed")
                self.assertEqual(result["error_type"], "ValueError")
                self.assertTrue(result["finished_utc"])


if __name__ == "__main__":
    unittest.main()
