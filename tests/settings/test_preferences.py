"""Settings recovery, frozen drag recipes, real publication and local result history."""

from __future__ import annotations

from dataclasses import asdict, replace
import hashlib
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication, QDialog, QMessageBox

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import DropSelection, prepare_request
from magic_converter.core.conversion.converter import ConversionService, ConversionWorker, find_ffmpeg, convert_media
from magic_converter.core.conversion.file_router import ALL_ACTIONS
from magic_converter.core.settings.history import HistoryStore
from magic_converter.core.jobs.job_models import FileResult, JobResult
from magic_converter.core.settings.json_store import read_json, write_json
from magic_converter.core.settings.preferences import (EncoderOptions, OutputRule, Preset, Preferences, SettingsError,
                                               WheelProfile, default_preferences)
from magic_converter.core.settings.settings_store import SettingsStore
from magic_converter.core.settings.wheel_profiles import profile_slots
from magic_converter.ui.shell.radial_wheel import RadialWheelWindow
from magic_converter.ui.settings.settings_dialog import SettingsDialog
from magic_converter.ui.jobs.history_dialog import HistoryDialog

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def pump_until(predicate, timeout=10):
    until = time.monotonic() + timeout
    while not predicate():
        APP.processEvents()
        if time.monotonic() > until:
            raise AssertionError("Timed out waiting for result")
        time.sleep(0.005)
    APP.processEvents()


class PreferencesTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.store = SettingsStore(self.root / "settings.json")

    def tearDown(self):
        APP.processEvents()
        self.temp.cleanup()

    def test_registry_values_migrate_once_and_json_takes_precedence(self):
        pass
        self.assertEqual(first.threshold, 21)
        pass
        self.assertEqual(first.interval_ms, 35)
        self.assertTrue(first.presets)
        self.assertEqual(self.store.load({"threshold": 77}), first)
        self.assertIn("복구", self.store.notice)

    def test_atomic_save_backup_and_corrupt_primary_recovery(self):
        first = self.store.load()
        second = replace(first, threshold=24)
        self.store.save(second)
        self.assertEqual(self.store.read_backup(), first)
        self.store.path.write_text("{broken", encoding="utf-8")
        recovered = SettingsStore(self.store.path)
        self.assertEqual(recovered.load(), first)
        self.assertIn("백업", recovered.notice)
        self.assertEqual(len(list(self.root.glob("settings.json.corrupt-*"))), 1)
        self.assertEqual(Preferences.from_dict(read_json(self.store.path)), first)

    def test_broken_backup_uses_defaults_but_keeps_damaged_files(self):
        self.store.path.write_bytes(b"bad settings")
        self.store.backup.write_bytes(b"bad backup")
        self.assertEqual(self.store.load(), default_preferences())
        self.assertEqual(self.store.backup.read_bytes(), b"bad backup")
        self.assertTrue(list(self.root.glob("*.corrupt-*")))

    def test_missing_primary_recovers_from_backup_without_remigrating_registry(self):
        first = self.store.load({"threshold": 27})
        self.store.save(replace(first, threshold=34))
        self.store.path.unlink()
        self.assertEqual(self.store.load({"threshold": 77}), first)
        self.assertIn("백업", self.store.notice)

    def test_future_settings_are_not_downgraded_or_overwritten(self):
        write_json(self.store.path, {"version": 500, "future": True})
        before = self.store.path.read_bytes()
        self.assertEqual(self.store.load(), default_preferences())
        with self.assertRaises(SettingsError):
            self.store.save(default_preferences())
        self.assertEqual(self.store.path.read_bytes(), before)

    def test_failed_atomic_publication_keeps_last_saved_preferences(self):
        self.store.load()
        before = self.store.path.read_bytes()
        original = os.replace
        def fail(source, destination):
            if Path(destination) == self.store.path:
                raise PermissionError("locked")
            return original(source, destination)
        with patch("magic_converter.core.settings.json_store.os.replace", side_effect=fail):
            with self.assertRaises(PermissionError):
                self.store.save(replace(default_preferences(), threshold=32))
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(list(self.root.glob(".magic-json-*")))

    def test_schema_rejects_invalid_recipes_references_and_unsafe_names(self):
        for changes in ({"threshold": True}, {"history_limit": 1001},
                        {"presets": [{"id": "broken", "name": "Bad", "action_key": "unknown"}]},
                        {"convert_profile": "not-present"}):
            with self.subTest(changes=changes), self.assertRaises(SettingsError):
                Preferences.from_dict({**default_preferences().to_dict(), **changes})
        for template in ("../{stem}", "{stem.__class__}", "{stem!r}", "{unknown}", "CON", "{stem}."):
            with self.subTest(template=template), self.assertRaises(SettingsError):
                OutputRule(template=template)
        with self.assertRaises(SettingsError):
            OutputRule("folder", "relative")
        with self.assertRaises(SettingsError):
            Preset("bad", "Bad", "document:pdf", EncoderOptions(image_max_edge=100))
        with self.assertRaises(SettingsError):
            WheelProfile("empty", "Empty", "convert", ())
        path = self.root / "large.json"
        path.write_bytes(b" " * 101)
        with self.assertRaises(ValueError):
            read_json(path, 100)

    def test_editor_duplicate_order_delete_export_and_cancel_are_isolated(self):
        initial = default_preferences()
        self.store.save(initial)
        dialog = SettingsDialog(initial.base_values(), preferences=initial, settings_store=self.store)
        editor = dialog.editor
        editor.preset_name.setText("웹용 사진")
        editor.quality.setValue(75)
        editor.edge.setValue(320)
        editor._copy_preset()
        copy_id = editor._data["presets"][-1]["id"]
        editor.profile_list.setCurrentRow(0)
        # Reorder checked slots using the same list model as drag and drop.
        item = editor.members.takeItem(1)
        editor.members.insertItem(0, item)
        editor.convert_profile.setCurrentIndex(editor.convert_profile.findData("images"))
        changed = dialog.values()["preferences"]
        self.assertEqual(changed.presets[0].name, "웹용 사진")
        self.assertEqual(changed.presets[0].options.image_max_edge, 320)
        self.assertNotEqual(copy_id, changed.presets[0].id)
        self.assertEqual(changed.profiles[0].preset_ids[0], "png")
        export = self.root / "export.json"
        with patch("magic_converter.ui.settings.settings_dialog.QFileDialog.getSaveFileName", return_value=(str(export), "")):
            dialog._export()
        self.assertEqual(Preferences.from_dict(read_json(export)), changed)
        dialog.reject()
        self.assertEqual(self.store.load(), initial)
        dialog.deleteLater()

    def test_import_and_restore_are_drafts_until_save(self):
        first = self.store.load()
        second = replace(first, threshold=33, output=OutputRule("folder", str(self.root / "outputs")))
        self.store.save(second)
        dialog = SettingsDialog(second.base_values(), preferences=second, settings_store=self.store)
        dialog._restore()
        self.assertEqual(dialog.values()["preferences"], first)
        self.assertEqual(self.store.load(), second)
        exported = self.root / "import.json"
        write_json(exported, second.to_dict())
        with patch("magic_converter.ui.settings.settings_dialog.QFileDialog.getOpenFileName", return_value=(str(exported), "")):
            dialog._import()
        self.assertEqual(dialog.values()["preferences"], second)
        self.assertEqual(dialog.editor.output.directory.text(), str(self.root / "outputs"))
        dialog._reset()
        self.assertEqual(dialog.values()["preferences"], default_preferences())
        dialog.deleteLater()

    def test_deleting_a_recipe_repairs_profile_references_and_keeps_stable_ids(self):
        initial = replace(default_preferences(), convert_profile="images")
        dialog = SettingsDialog(initial.base_values(), preferences=initial)
        editor = dialog.editor
        editor._delete_preset()
        result = dialog.values()["preferences"]
        self.assertNotIn("jpg", {p.id for p in result.presets})
        self.assertEqual(result.profiles[0].preset_ids, ("png", "webp", "pdf"))
        self.assertEqual(result.convert_profile, "images")
        editor.preset_output_group.setChecked(True)
        editor.preset_output.template.setText("../invalid")
        editor.preset_list.setCurrentRow(2)
        editor.preset_list.setCurrentRow(0)
        with self.assertRaises(SettingsError):
            dialog.values()
        dialog.deleteLater()

    def test_manager_persists_recipes_records_results_and_rolls_back_failed_save(self):
        from PySide6.QtCore import QSettings
        from PySide6.QtWidgets import QSystemTrayIcon
        from magic_converter.main import ApplicationManager
        from magic_converter.core.platform.hook_manager import HookManager, InputState
        settings = QSettings(str(self.root / "legacy.ini"), QSettings.Format.IniFormat)
        idle = lambda: InputState(False, False, False, 0, 0)
        with patch("magic_converter.main.QSettings", return_value=settings), patch(
                "magic_converter.main.HookManager", side_effect=lambda parent, **kwargs: HookManager(parent, reader=idle, **kwargs)), patch.object(
                    QSystemTrayIcon, "show"), patch.object(QSystemTrayIcon, "showMessage"):
            manager = ApplicationManager(APP)
            changed = replace(manager.preferences, convert_profile="images", threshold=26,
                              output=OutputRule("folder", str(self.root / "results")))
            manager._apply_settings({**changed.base_values(), "preferences": changed})
            self.assertEqual(manager.settings_store.load(), changed)
            self.assertEqual(manager.service.preferences, changed)
            self.assertEqual(settings.value("threshold", type=int), 26)
            source = self.root / "source.png"
            Image.new("RGB", (40, 20), "orange").save(source)
            manager.service.submit((source,), "image:jpg")
            pump_until(lambda: manager.service.active_count == 0)
            self.assertEqual(len(manager.history.entries), 1)
            self.assertEqual(manager.history.entries[0]["output"], asdict(changed.output))
            with patch.object(manager.settings_store, "save", side_effect=PermissionError("read only")):
                manager._apply_settings({"threshold": 45})
            self.assertEqual(manager.preferences, changed)
            self.assertEqual(manager.hook.threshold, 26)
            self.assertEqual(manager.tray.values["threshold"], 26)
            manager.service.pool.waitForDone(3000)
            manager._cleanup()
            manager.wheel.deleteLater()
            manager.progress_overlay.deleteLater()
            manager.deleteLater()
            APP.processEvents()


class RecipeExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "사진.png"
        Image.new("RGB", (120, 60), "orange").save(self.source)
        pass

    def tearDown(self):
        APP.processEvents()
        self.temp.cleanup()

    def execute(self, paths, key, **options):
        request = prepare_request(paths, key, self.catalog, **options)
        worker = ConversionWorker(1, request.paths, request.action, None, request=request)
        results = []
        worker.signals.finished.connect(results.append)
        worker.run()
        return results[0]

    def test_preset_resize_quality_and_named_output_reach_the_real_encoder(self):
        folder = self.root / "별도 출력"
        rule = OutputRule("folder", str(folder), "{stem}_{preset}")
        preset = Preset("small", "웹 사진", "image:jpg", EncoderOptions(72, 40), rule)
        digest = hashlib.sha256(self.source.read_bytes()).digest()
        result = self.execute((self.source,), "image:jpg", preset=preset)
        self.assertFalse(result.errors)
        output = folder / "사진_웹 사진.jpg"
        self.assertEqual(result.outputs, [output])
        import io
        expected = io.BytesIO()
        Image.new("RGB", (1, 1)).save(expected, format="JPEG", quality=72)
        with Image.open(output) as decoded:
            self.assertEqual(decoded.size, (40, 20))
            with Image.open(expected) as reference:
                self.assertEqual(decoded.quantization, reference.quantization)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), digest)
        self.assertEqual(list(self.root.glob("*_converted*")), [])

    def test_output_collision_policy_and_failure_do_not_replace_existing_files(self):
        folder = self.root / "outputs"
        folder.mkdir()
        existing = folder / "写真.jpg"
        existing.write_bytes(b"keep original output")
        rule = OutputRule("folder", str(folder), "写真", "rename")
        result = self.execute((self.source,), "image:jpg", output=rule)
        self.assertEqual(result.outputs[0].name, "写真(1).jpg")
        with self.assertLogs("magic_converter.core.conversion.converter", level="ERROR"):
            failed = self.execute((self.source,), "image:jpg", output=replace(rule, collision="error"))
        self.assertEqual(failed.files[0].state, "failed")
        self.assertEqual(existing.read_bytes(), b"keep original output")
        self.assertFalse(list(folder.glob(".magic*")))

    def test_multi_hop_folder_and_combined_zip_use_the_chosen_output_rule(self):
        text = self.root / "notes.txt"
        text.write_text("One page", encoding="utf-8")
        rule = OutputRule("folder", str(self.root / "outputs"), "{stem}_result")
        pages = self.execute((text,), "document:png", output=rule)
        self.assertFalse(pages.errors)
        self.assertEqual(pages.outputs[0], self.root / "outputs" / "notes_result")
        self.assertEqual(len(list(pages.outputs[0].glob("*.png"))), 1)
        zipped = self.execute((text, self.source), "zip", output=rule)
        self.assertFalse(zipped.errors)
        self.assertEqual(zipped.outputs[0], self.root / "outputs" / "notes_result.zip")
        self.assertFalse((self.root / "notes_converted.pdf").exists())

    def test_profile_slot_order_and_disabled_reason_use_the_actual_action(self):
        preferences = replace(default_preferences(), convert_profile="documents")
        slots = profile_slots((self.source,), "convert", preferences, False, False, False)
        self.assertEqual([slot.preset.id for slot in slots], ["pdf", "docx", "txt", "pages"])
        self.assertTrue(slots[0].enabled)
        self.assertFalse(slots[1].enabled)
        self.assertTrue(slots[1].reason)
        request = prepare_request((self.source,), slots[0].action.key, self.catalog, preset=slots[0].preset)
        self.assertEqual(request.action.label, slots[0].action.label)

    def test_drag_freezes_profile_options_and_output_even_when_settings_change(self):
        rule = OutputRule("folder", str(self.root / "before"))
        recipe = Preset("small", "작은 사진", "image:jpg", EncoderOptions(70, 30))
        initial = Preferences(presets=(recipe,), profiles=(WheelProfile("photo", "사진", "convert", ("small",)),),
                              convert_profile="photo", output=rule)
        current = [initial]
        wheel = RadialWheelWindow(lambda: False, preferences=lambda: current[0])
        wheel.show_for_drag("convert", QPoint(500, 500))
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(self.source))])
        enter = QDragEnterEvent(QPoint(160, 60), Qt.DropAction.CopyAction, mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        wheel.dragEnterEvent(enter)
        current[0] = replace(initial, presets=(replace(recipe, action_key="image:png", options=EncoderOptions()),),
                             output=replace(rule, directory=str(self.root / "after")))
        selections = []
        wheel.request_dropped.connect(selections.append)
        drop = QDropEvent(QPointF(160, 60), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        wheel.dropEvent(drop)
        self.assertTrue(drop.isAccepted())
        self.assertEqual(selections, [DropSelection((self.source,), "image:jpg", recipe, rule)])
        result = self.execute(selections[0].paths, selections[0].action_key,
                              preset=selections[0].preset, output=selections[0].output)
        self.assertEqual(result.outputs[0].parent, self.root / "before")
        with Image.open(result.outputs[0]) as image:
            self.assertEqual(image.size, (30, 15))
        wheel.deleteLater()

    def test_drop_rejects_a_changed_payload_instead_of_remapping_a_visible_slot(self):
        wheel = RadialWheelWindow(lambda: False)
        wheel.show_for_drag("convert", QPoint(500, 500))
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(self.source))])
        wheel.dragEnterEvent(QDragEnterEvent(QPoint(160, 60), Qt.DropAction.CopyAction, mime,
                                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier))
        selections = []
        wheel.request_dropped.connect(selections.append)
        mime.setUrls([QUrl.fromLocalFile(str(self.root / "changed.pdf"))])
        drop = QDropEvent(QPointF(160, 60), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        wheel.dropEvent(drop)
        self.assertFalse(drop.isAccepted())
        self.assertFalse(selections)
        wheel.deleteLater()

    def test_queued_request_keeps_its_output_snapshot_after_global_settings_change(self):
        service = ConversionService(parallelism=1)
        results = []
        service.completed.connect(results.append)
        before = replace(default_preferences(), output=OutputRule("folder", str(self.root / "before")))
        service.preferences = before
        with patch("magic_converter.core.conversion.builtin_converters.runtime_catalog", return_value=self.catalog):
            service.submit((self.source,), "image:jpg")
            service.submit((self.source,), "image:jpg")
            service.preferences = replace(before, output=OutputRule("folder", str(self.root / "after")))
            pump_until(lambda: len(results) == 2)
        self.assertTrue(all(result.outputs[0].parent == self.root / "before" for result in results))
        self.assertEqual(len(set(result.outputs[0] for result in results)), 2)
        service.pool.waitForDone(3000)
        service.deleteLater()

    @unittest.skipUnless(find_ffmpeg(), "FFmpeg unavailable")
    def test_media_recipe_crf_and_audio_bitrate_reach_ffmpeg(self):
        from magic_converter.core.jobs.process_runner import run_process
        from magic_converter.core.conversion.converter import find_ffprobe
        ffmpeg = find_ffmpeg()
        source = self.root / "source.mp4"
        generated = run_process([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
            "-f", "lavfi", "-i", "testsrc2=size=64x48:rate=10:duration=0.3", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.3", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)], timeout=10)
        self.assertEqual(generated.returncode, 0)
        output = self.root / "recipe.mp4"
        with patch("magic_converter.core.jobs.process_runner.run_process", wraps=run_process) as calls:
            convert_media(source, output, "media:mp4", ffmpeg, options=EncoderOptions(video_crf=28, audio_bitrate=64))
        command = calls.call_args.args[0]
        self.assertEqual(command[command.index("-crf") + 1], "28")
        self.assertEqual(command[command.index("-b:a") + 1], "64k")
        probe = run_process([find_ffprobe(ffmpeg), "-v", "error", "-show_entries", "stream=codec_type", "-of", "json", str(output)], timeout=10)
        self.assertEqual({stream["codec_type"] for stream in json.loads(probe.stdout)["streams"]}, {"audio", "video"})


class HistoryTests(unittest.TestCase):
    def test_outcomes_retention_disable_reload_and_clear_preserve_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "keep.jpg"
            output.write_bytes(b"keep")
            store = HistoryStore(root / "history.json", limit=2)
            result = JobResult(1, ALL_ACTIONS["image:jpg"], 4, files=[
                FileResult(root / "good.png", "done", (output,), warnings=("Warning",)),
                FileResult(root / "bad.png", "failed", errors=("Error",)),
                FileResult(root / "cancel.png", "cancelled"), FileResult(root / "slow.png", "timed_out")])
            for _ in range(3):
                store.append(result)
            reloaded = HistoryStore(store.path)
            self.assertEqual(len(reloaded.entries), 2)
            self.assertEqual([file["state"] for file in reloaded.entries[0]["files"]], ["done", "failed", "cancelled", "timed_out"])
            self.assertEqual(reloaded.entries[0]["files"][0]["warnings"], ["Warning"])
            self.assertEqual(reloaded.entries[0]["files"][1]["errors"], ["Error"])
            store.limit = 0
            before = store.path.read_bytes()
            store.append(result)
            self.assertEqual(store.path.read_bytes(), before)
            dialog = HistoryDialog(reloaded)
            dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0).child(0))
            self.assertTrue(dialog.open_button.isEnabled())
            with patch("magic_converter.ui.jobs.history_dialog.QDesktopServices.openUrl") as open_url:
                dialog._open()
            self.assertEqual(Path(open_url.call_args.args[0].toLocalFile()), root)
            dialog.tree.setCurrentItem(dialog.tree.topLevelItem(0).child(1))
            self.assertIn("Error", dialog.detail.toPlainText())
            dialog.refresh()
            self.assertIn("Error", dialog.detail.toPlainText())
            self.assertFalse(dialog.open_button.isEnabled())
            with patch("magic_converter.ui.jobs.history_dialog.QMessageBox.question", return_value=QMessageBox.StandardButton.Yes):
                dialog._clear()
            self.assertEqual(reloaded.entries, [])
            self.assertEqual(output.read_bytes(), b"keep")
            dialog.reject()
            APP.processEvents()

    def test_corrupt_history_is_preserved_before_new_results_are_recorded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "history.json"
            path.write_bytes(b"corrupt history")
            store = HistoryStore(path)
            self.assertTrue(store.notice)
            self.assertEqual(path.read_bytes(), b"corrupt history")
            self.assertEqual(len(list(path.parent.glob("history.json.corrupt-*"))), 1)
            store.append(JobResult(1, ALL_ACTIONS["zip"], 1, files=[FileResult(path.parent / "source.txt", "failed")]))
            self.assertEqual(len(HistoryStore(path).entries), 1)


if __name__ == "__main__":
    unittest.main()
