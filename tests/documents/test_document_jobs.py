"""Stalled Office jobs cannot starve other conversion queues."""

import os
import json
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication
from magic_converter.core.conversion.converter import ConversionError, ConversionService, ConversionWorker
from magic_converter.core.conversion.file_router import ALL_ACTIONS
from magic_converter.core.jobs.job_control import Control, JobCancelled, check_cancelled, current
from magic_converter.core.engines.office_process import run_helper

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class OfficeJobTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_stage_reports_preserve_the_owned_process_ledger(self):
        from magic_converter.core.engines.office_com import ProcessLedger
        path = self.root / "ledger.json"
        ledger = ProcessLedger(path)
        ledger.mark("open")
        self.assertEqual(json.loads(path.read_text())["owned"], [])
        self.assertEqual(json.loads(path.with_suffix(".state.json").read_text())["stage"], "open")

    def test_cancelled_queued_worker_does_not_touch_the_source_or_create_output(self):
        source = self.root / "source.txt"
        source.write_text("original", encoding="utf-8")
        worker = ConversionWorker(1, (source,), ALL_ACTIONS["document:md"], None)
        results = []
        worker.signals.finished.connect(results.append)
        worker.cancel_event.set()
        worker.run()
        self.assertTrue(results[0].cancelled)
        self.assertFalse(results[0].errors)
        self.assertEqual(source.read_text(), "original")
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_office_stall_leaves_txt_md_conversion_running_and_cancel_clears_both_queues(self):
        from magic_converter.core.conversion.conversion_execution import run_legacy_action as real_convert
        native = self.root / "sample.hwpx"
        native.write_bytes(b"mocked native input")
        text = self.root / "notes.txt"
        text.write_text("hello queue", encoding="utf-8")
        entered, release = threading.Event(), threading.Event()
        def fake_convert(step, progress, warning):
            if step.source.suffix == ".hwpx":
                entered.set()
                while not release.wait(0.01):
                    check_cancelled()
                check_cancelled()
                return step.source
            return real_convert(step, progress, warning)
        service, results = ConversionService(), []
        service.completed.connect(results.append)
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=fake_convert):
            try:
                service.submit([native], "document:docx")
                service.submit([native], "document:pdf")
                self.assertTrue(entered.wait(1))
                service.submit([text], "document:md")
                deadline = time.monotonic() + 2
                while not any(r.action.key == "document:md" for r in results) and time.monotonic() < deadline:
                    APP.processEvents()
                    time.sleep(0.005)
                self.assertTrue(any(r.action.key == "document:md" and r.completed == 1 for r in results))
                self.assertEqual(service.active_count, 2)
                service.cancel_all()
                deadline = time.monotonic() + 2
                while service.active_count and time.monotonic() < deadline:
                    APP.processEvents()
                    time.sleep(0.005)
                self.assertEqual(service.active_count, 0)
                self.assertEqual(sum(r.cancelled for r in results), 2)
            finally:
                release.set()
                service.pool.waitForDone(3000)
                service.office_pool.waitForDone(3000)

    def test_real_child_timeout_cannot_deadlock_on_inherited_stderr_pipe(self):
        child = self.root / "child.py"
        child.write_text("import subprocess,sys,time\nsubprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'],stderr=sys.stderr)\ntime.sleep(30)\n", encoding="utf-8")
        start = time.monotonic()
        with self.assertRaises(TimeoutError):
            run_helper([sys.executable, str(child)], self.root / "state.json", timeout=0.4)
        self.assertLess(time.monotonic() - start, 3)

    def test_cancel_stops_real_helper_without_waiting_for_its_timeout(self):
        event = threading.Event()
        timer = threading.Timer(0.3, event.set)
        token = current.set(Control(event, lambda _: None))
        start = time.monotonic()
        timer.start()
        try:
            with self.assertRaises(JobCancelled):
                run_helper([sys.executable, "-c", "import time;time.sleep(30)"], self.root / "state.json")
        finally:
            timer.cancel()
            current.reset(token)
        self.assertLess(time.monotonic() - start, 3)


if __name__ == "__main__":
    unittest.main()
