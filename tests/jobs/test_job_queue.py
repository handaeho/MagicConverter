"""Concurrency, fairness, isolated failures and selective cancellation."""

from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import QApplication

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_catalog import ConverterCatalog
from magic_converter.core.conversion.conversion_execution import run_legacy_action
from magic_converter.core.conversion.converter import ConversionService
from magic_converter.core.conversion.file_router import ALL_ACTIONS
from magic_converter.core.jobs.job_control import check_cancelled

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
for font_name in ("segoeui.ttf", "malgun.ttf"):
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font_name
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))


def pump_until(predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        APP.processEvents()
        if time.monotonic() >= deadline:
            raise AssertionError("Timed out waiting for scheduled work")
        time.sleep(0.005)
    APP.processEvents()


class FileQueueTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = create_catalog(ffmpeg_available=False, presentation_available=False)
        self.catalog_patch = patch("magic_converter.core.conversion.builtin_converters.runtime_catalog", side_effect=lambda _: self.catalog)
        self.catalog_patch.start()
        self.services = []

    def tearDown(self):
        for service in self.services:
            service.cancel_all()
            pump_until(lambda: service.active_count == 0)
            service.pool.waitForDone(3000)
            service.office_pool.waitForDone(3000)
            service.deleteLater()
        APP.processEvents()
        self.catalog_patch.stop()
        self.temporary.cleanup()

    def image(self, name):
        path = self.root / (name + ".png")
        Image.new("RGB", (48, 32), "orange").save(path)
        return path

    def service(self, **options):
        service = ConversionService(**options)
        results = []
        service.completed.connect(results.append)
        self.services.append(service)
        return service, results

    def limit_images(self, limit):
        self.catalog = ConverterCatalog(
            (replace(engine, max_parallelism=limit) if engine.id == "images" else engine
             for engine in self.catalog.converters),
            policies={key: self.catalog.policy(key) for key in ALL_ACTIONS})

    @staticmethod
    def wait(release):
        while not release.wait(0.01):
            check_cancelled()
        check_cancelled()

    def test_two_files_in_one_batch_run_concurrently_and_keep_input_order(self):
        sources = [self.image("first"), self.image("second")]
        release = threading.Event()
        entered, lock = [], threading.Lock()

        def convert(step, progress, notice):
            with lock:
                entered.append(step.source)
            self.wait(release)
            return run_legacy_action(step, progress, notice)

        service, results = self.service()
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=convert):
            try:
                service.submit(sources, "image:jpg")
                pump_until(lambda: len(entered) == 2)
                self.assertEqual(service.active_count, 1)
                self.assertEqual(service.active_file_count, 2)
                release.set()
                pump_until(lambda: bool(results))
            finally:
                release.set()
        self.assertEqual([file.source for file in results[0].files], sources)
        self.assertEqual([file.state for file in results[0].files], ["done", "done"])
        self.assertEqual([file.name for file in results[0].outputs], ["first_converted.jpg", "second_converted.jpg"])

    def test_a_missing_file_does_not_abort_other_files_in_the_batch(self):
        source, missing = self.image("good"), self.root / "missing.png"
        service, results = self.service()
        with self.assertLogs("magic_converter.core.conversion.converter", level="ERROR"):
            service.submit((source, missing), "image:jpg")
            pump_until(lambda: bool(results))
        self.assertEqual(results[0].completed, 1)
        self.assertEqual([file.state for file in results[0].files], ["done", "failed"])
        self.assertIn("missing.png", results[0].errors[0])
        self.assertTrue(results[0].outputs[0].is_file())

    def test_engine_budget_skips_waiting_files_instead_of_blocking_a_pool_thread(self):
        self.limit_images(1)
        first, second = self.image("first"), self.image("second")
        text = self.root / "notes.txt"
        text.write_text("Another engine can proceed", encoding="utf-8")
        entered, release = [], threading.Event()

        def convert(step, progress, notice):
            entered.append(step.source)
            if step.source == first:
                self.wait(release)
            return run_legacy_action(step, progress, notice)

        service, results = self.service()
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=convert):
            try:
                service.submit((first, second, text), "document:pdf")
                pump_until(lambda: service.file_results(1)[2].state == "done")
                self.assertEqual([file.state for file in service.file_results(1)], ["running", "queued", "done"])
                self.assertNotIn(second, entered)
                release.set()
                pump_until(lambda: bool(results))
            finally:
                release.set()
        self.assertEqual(results[0].completed, 3)

    def test_office_and_image_files_in_the_same_batch_use_separate_pools(self):
        native = self.root / "native.docx"
        native.write_bytes(b"mocked office input")
        image = self.image("picture")
        release, entered = threading.Event(), threading.Event()

        def convert(step, progress, notice):
            if step.source == native:
                entered.set()
                self.wait(release)
                from magic_converter.core.conversion.converter import output_file
                with output_file(native, ".pdf") as (temporary, destination):
                    temporary.write_bytes(b"mocked completed output")
                return destination
            return run_legacy_action(step, progress, notice)

        service, results = self.service()
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=convert):
            try:
                service.submit((native, image), "document:pdf")
                pump_until(lambda: entered.is_set() and service.file_results(1)[1].state == "done")
                self.assertFalse(results)
                release.set()
                pump_until(lambda: bool(results))
            finally:
                release.set()
        self.assertEqual(results[0].completed, 2)

    def test_out_of_order_completion_does_not_report_a_finished_batch(self):
        first, second = self.image("slow"), self.image("fast")
        release = threading.Event()
        updates = []

        def convert(step, progress, notice):
            if step.source == first:
                progress(50)
                self.wait(release)
            return run_legacy_action(step, progress, notice)

        service, results = self.service()
        service.progress_changed.connect(updates.append)
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=convert):
            try:
                service.submit((first, second), "image:jpg")
                pump_until(lambda: service.file_results(1)[1].state == "done")
                self.assertFalse(results)
                self.assertEqual(updates[-1].overall_percent, 75)
                self.assertEqual(updates[-1].finished_files, 1)
                self.assertFalse(any(update.overall_percent == 100 for update in updates))
                release.set()
                pump_until(lambda: bool(results))
            finally:
                release.set()
        amounts = [update.overall_percent for update in updates if update.overall_percent is not None]
        self.assertEqual(amounts, sorted(amounts))
        self.assertEqual(amounts[-1], 100)

    def test_selective_cancellation_stops_running_and_queued_files_only(self):
        self.limit_images(1)
        sources = [self.image(name) for name in ("running", "queued", "keep")]
        release, entered = threading.Event(), []

        def convert(step, progress, notice):
            entered.append(step.source)
            if step.source == sources[0]:
                self.wait(release)
            return run_legacy_action(step, progress, notice)

        service, results = self.service()
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=convert):
            try:
                service.submit(sources, "image:jpg")
                pump_until(lambda: sources[0] in entered)
                self.assertTrue(service.cancel_file(1, 2))
                self.assertEqual(service.file_results(1)[1].state, "cancelled")
                self.assertTrue(service.cancel_file(1, 1))
                self.assertTrue(service.file_results(1)[0].cancel_requested)
                from magic_converter.ui.jobs.job_manager import JobManagerDialog
                dialog = JobManagerDialog(service)
                dialog.tree.topLevelItem(0).child(0).setSelected(True)
                self.assertEqual(dialog.tree.topLevelItem(0).child(0).text(1), "취소 요청 중")
                self.assertFalse(dialog.cancel_file_button.isEnabled())
                dialog.reject()
                pump_until(lambda: bool(results))
            finally:
                release.set()
        self.assertEqual([file.state for file in results[0].files], ["cancelled", "cancelled", "done"])
        self.assertEqual(results[0].completed, 1)
        self.assertTrue(results[0].cancelled)
        self.assertNotIn(sources[1], entered)
        self.assertFalse((self.root / "running_converted.jpg").exists())
        self.assertFalse((self.root / "queued_converted.jpg").exists())
        self.assertFalse(service.cancel_file(1, 1))

    def test_cancel_one_job_preserves_another_job_and_releases_budgets(self):
        first, second = self.image("cancel"), self.image("keep")
        entered, release = threading.Event(), threading.Event()

        def convert(step, progress, notice):
            if step.source == first:
                entered.set()
                self.wait(release)
            return run_legacy_action(step, progress, notice)

        service, results = self.service(parallelism=1)
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=convert):
            try:
                service.submit((first,), "image:jpg")
                service.submit((second,), "image:jpg")
                pump_until(entered.is_set)
                self.assertTrue(service.cancel_job(1))
                pump_until(lambda: len(results) == 2)
            finally:
                release.set()
        outcomes = {result.job_id: result for result in results}
        self.assertTrue(outcomes[1].cancelled)
        self.assertEqual(outcomes[2].completed, 1)
        self.assertEqual(service.active_count, 0)
        self.assertFalse(service.cancel_job(999))

    def test_a_large_batch_yields_a_slot_to_a_later_job(self):
        sources = [self.image(name) for name in ("a0", "a1", "a2", "b")]
        release, entered = threading.Event(), []

        def convert(step, progress, notice):
            entered.append(step.source)
            if step.source == sources[0]:
                self.wait(release)
            return run_legacy_action(step, progress, notice)

        service, results = self.service(parallelism=1)
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=convert):
            try:
                service.submit(sources[:3], "image:jpg")
                pump_until(lambda: bool(entered))
                service.submit((sources[3],), "image:jpg")
                release.set()
                pump_until(lambda: len(results) == 2)
            finally:
                release.set()
        self.assertEqual(entered[:2], [sources[0], sources[3]])

    def test_file_timeout_excludes_queue_wait_and_does_not_poison_next_file(self):
        first, second = self.image("timeout"), self.image("next")

        def convert(step, progress, notice):
            if step.source == first:
                while True:
                    time.sleep(0.01)
                    check_cancelled()
            # This scheduler test needs a deterministic sub-250ms encoder.
            # Real library worker startup/deadlines have separate integration tests.
            from magic_converter.core.conversion.conversion_execution import run_local_action
            from magic_converter.core.jobs.raw_api import in_worker
            token = in_worker.set(True)
            try:
                return run_local_action(step, progress, notice)
            finally:
                in_worker.reset(token)

        service, results = self.service(parallelism=1, file_timeout=0.25)
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=convert):
            service.submit((first, second), "image:jpg")
            pump_until(lambda: bool(results))
        self.assertTrue(results[0].timed_out)
        self.assertFalse(results[0].cancelled)
        self.assertEqual([file.state for file in results[0].files], ["timed_out", "done"])
        self.assertEqual(results[0].completed, 1)
        self.assertFalse((self.root / "timeout_converted.jpg").exists())
        self.assertTrue((self.root / "next_converted.jpg").exists())

    def test_job_manager_can_cancel_a_selected_queued_file(self):
        from magic_converter.ui.jobs.job_manager import JobManagerDialog
        first, second = self.image("실행 중"), self.image("대기 파일")
        release, entered = threading.Event(), threading.Event()

        def convert(step, progress, notice):
            if step.source == first:
                entered.set()
                self.wait(release)
            return run_legacy_action(step, progress, notice)

        service, results = self.service(parallelism=1)
        dialog = JobManagerDialog(service)
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=convert):
            try:
                service.submit((first, second), "image:jpg")
                pump_until(entered.is_set)
                dialog.refresh()
                dialog.show()
                dialog.tree.topLevelItem(0).child(1).setSelected(True)
                self.assertTrue(dialog.cancel_file_button.isEnabled())
                APP.processEvents()
                artifact = Path(".test-artifacts") / "jobs-stage-2.png"
                artifact.parent.mkdir(exist_ok=True)
                self.assertTrue(dialog.grab().save(str(artifact)))
                dialog.cancel_file_button.click()
                self.assertEqual(service.file_results(1)[1].state, "cancelled")
                self.assertFalse(dialog.cancel_file_button.isEnabled())
                release.set()
                pump_until(lambda: bool(results))
                dialog.refresh()
                self.assertEqual(dialog.tree.topLevelItemCount(), 0)
                self.assertFalse(dialog.cancel_all_button.isEnabled())
            finally:
                release.set()
                dialog.reject()
        self.assertEqual([file.state for file in results[0].files], ["done", "cancelled"])


if __name__ == "__main__":
    unittest.main()
