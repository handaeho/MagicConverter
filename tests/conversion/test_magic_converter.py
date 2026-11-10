"""Run: python -m unittest discover -s tests -v (Windows, Qt offscreen).

Set MAGIC_CONVERTER_FFMPEG to also run real video/audio integration checks.
"""

from __future__ import annotations

import hashlib
import math
import os
import random
import subprocess
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent, QFontDatabase
from PySide6.QtWidgets import QApplication, QSystemTrayIcon

from magic_converter.core.conversion.converter import ConversionError, ConversionService, ConversionWorker, FFmpegProgressParser, JobProgress, JobResult, convert_image, convert_media, create_zip, find_ffmpeg, open_terminals, output_file, probe_duration
from magic_converter.core.conversion.file_router import ALL_ACTIONS, normalize_paths, slots_for
from magic_converter.core.platform.hook_manager import HookManager, InputState, WindowsInput
from magic_converter.ui.shell.radial_wheel import RadialWheelWindow, slice_at
from magic_converter.ui.settings.settings_dialog import SettingsDialog
from magic_converter.ui.shell.tray_icon import TrayIcon
from magic_converter.ui.jobs.progress_overlay import ProgressOverlay

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
# Qt's offscreen Windows platform has no native font database. Explicitly
# load installed fonts for visual QA; the normal Windows platform uses its
# native font database and requires no bundled or downloaded font assets.
for font_name in ("segoeui.ttf", "malgun.ttf"):
    font_path = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / font_name
    if font_path.is_file():
        QFontDatabase.addApplicationFont(str(font_path))


def pump_until(predicate, timeout: float = 12) -> None:
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("Timed out waiting for Qt worker signal")
        APP.processEvents()
        time.sleep(0.005)
    APP.processEvents()


class RoutingAndGeometryTests(unittest.TestCase):
    def test_progress_parser_units_monotonicity_and_invalid_values(self):
        parser = FFmpegProgressParser(10)
        self.assertEqual(parser.feed("out_time_us=2500000"), 25)
        self.assertIsNone(parser.feed("out_time_ms=2500000"))
        self.assertIsNone(parser.feed("out_time_us=1000000"))
        self.assertEqual(parser.feed("out_time=00:00:08.500000"), 85)
        for invalid in ("out_time_us=N/A", "out_time_us=-9223372036854775808", "out_time=N/A", "other=42", "progress=end"):
            self.assertIsNone(parser.feed(invalid))
        self.assertEqual(parser.feed("out_time_us=12000000"), 99)
        self.assertIsNone(FFmpegProgressParser(None).feed("out_time_us=100"))
        self.assertEqual(JobProgress(1, "MP4", "f.mp4", 2, 4, 50).overall_percent, 37)

    def test_common_actions_and_ffmpeg_eligibility(self):
        audio = slots_for(["SOUND.WAV"], "convert")
        self.assertFalse(any(s.action.key == "media:gif" for s in audio))
        self.assertTrue(next(s for s in audio if s.action.key == "audio:flac").enabled)
        mixed = slots_for(["a.png", "b.mp3"], "action", ffmpeg_available=False)
        self.assertFalse(next(s for s in mixed if s.action.key == "strip").enabled)
        self.assertEqual({s.action.key for s in mixed if s.enabled}, {"copy_path", "zip", "terminal"})
        self.assertFalse(any(s.enabled for s in slots_for(["a.unknown"], "convert")))
        self.assertEqual(len(normalize_paths(["a.png", "a.png"])), 1)

    def test_clockwise_geometry_all_slot_counts(self):
        for count in (1, 4, 5, 6):
            for index in range(count):
                angle = math.tau * index / count - math.pi / 2
                self.assertEqual(slice_at(160 + 100 * math.cos(angle), 160 + 100 * math.sin(angle), count), index)
        self.assertEqual(slice_at(160, 160, 6), -1)
        self.assertEqual(slice_at(0, 0, 6), -1)

    def test_native_reader(self):
        if os.name == "nt":
            self.assertIsInstance(WindowsInput()(), InputState)

    def test_hook_trigger_switch_and_consumed_drop(self):
        state = [InputState(True, False, False, 0, 0)]
        hook = HookManager(reader=lambda: state[0])
        requests, dismissals = [], []
        hook.wheel_requested.connect(lambda mode, point: requests.append(mode))
        hook.dismiss_requested.connect(dismissals.append)
        hook.poll()
        state[0] = InputState(True, True, False, 20, 0)
        hook.poll()
        hook.poll()
        self.assertEqual(requests, ["convert"])
        state[0] = InputState(True, True, True, 20, 0)
        hook.poll()
        self.assertEqual(requests[-1], "action")
        hook.consume_drag()
        hook.poll()
        self.assertEqual(len(requests), 2)
        state[0] = InputState(False, False, False, 20, 0)
        hook.poll()
        state[0] = InputState(True, False, False, 0, 0)
        hook.poll()
        state[0] = InputState(True, True, False, 30, 0)
        hook.poll()
        state[0] = InputState(True, False, False, 30, 0)
        hook.poll()
        self.assertEqual(dismissals[-1], "modifier_released")
        state[0] = InputState(True, True, False, 30, 0, True)
        hook.poll()
        self.assertEqual(dismissals[-1], "escape")
        state[0] = InputState(True, True, False, 30, 0)
        hook.poll()
        self.assertEqual(len(requests), 3)


class FileAndWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "원본 image.png"
        Image.new("RGBA", (256, 192), (230, 110, 20, 90)).save(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def test_conversion_metadata_and_transparency(self):
        source = self.root / "oriented.jpg"
        exif = Image.Exif()
        exif[0x0112], exif[0x013B] = 6, "Private Author"
        Image.new("RGB", (80, 40), "red").save(source, exif=exif)
        for suffix in (".png", ".jpg", ".webp", ".bmp", ".tiff"):
            destination = self.root / ("result" + suffix)
            convert_image(source, destination, suffix)
            with Image.open(destination) as image:
                self.assertEqual(image.size, (40, 80))
                self.assertNotIn(0x013B, image.getexif())
        destination = self.root / "opaque.jpg"
        convert_image(self.source, destination, ".jpg")
        with Image.open(destination) as image:
            self.assertEqual(image.mode, "RGB")

    def test_half_size_verified_and_impossible_rolls_back(self):
        source = self.root / "noise.png"
        rng = random.Random(19)
        Image.frombytes("RGB", (256, 256), rng.randbytes(256 * 256 * 3)).save(source)
        destination = self.root / "half.png"
        convert_image(source, destination, ".png", compress=True)
        self.assertLessEqual(destination.stat().st_size, source.stat().st_size // 2)
        tiny = self.root / "tiny.png"
        Image.new("RGBA", (1, 1)).save(tiny)
        with self.assertRaises(ConversionError):
            with output_file(tiny, ".png") as (temporary, final):
                convert_image(tiny, temporary, ".png", compress=True)
        self.assertFalse(final.exists())
        self.assertFalse(list(self.root.glob(".magic_converter-*")))

    def test_concurrent_collision_and_atomic_failure(self):
        existing = self.root / "원본 image_converted.jpg"
        existing.write_bytes(b"keep me")

        def write(index):
            with output_file(self.source, ".jpg") as (temporary, destination):
                temporary.write_bytes(str(index).encode())
            return destination

        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(write, range(8)))
        self.assertEqual(len(set(outputs)), 8)
        self.assertEqual(existing.read_bytes(), b"keep me")
        with self.assertRaisesRegex(RuntimeError, "encoder"):
            with output_file(self.source, ".jpg") as (temporary, failed):
                temporary.write_bytes(b"partial")
                raise RuntimeError("encoder")
        self.assertFalse(failed.exists())

    def test_zip_duplicate_basenames(self):
        other_dir = self.root / "other"
        other_dir.mkdir()
        second = other_dir / self.source.name
        second.write_bytes(b"other file")
        output = create_zip((self.source, second))
        with zipfile.ZipFile(output) as archive:
            self.assertEqual(len(set(archive.namelist())), 2)
            self.assertEqual(sorted(len(archive.read(name)) for name in archive.namelist()),
                             sorted([self.source.stat().st_size, second.stat().st_size]))

    def test_animated_webp_frames_preserved(self):
        source = self.root / "animation.webp"
        first, second = Image.new("RGBA", (32, 32), "red"), Image.new("RGBA", (32, 32), "blue")
        first.save(source, save_all=True, append_images=[second], duration=[80, 120], loop=2)
        output = self.root / "animation.png"
        convert_image(source, output, ".png")
        with Image.open(output) as image:
            self.assertEqual(image.n_frames, 2)
            self.assertEqual(image.info["loop"], 2)
        with self.assertRaises(ConversionError):
            convert_image(source, self.root / "animation.jpg", ".jpg")

    def test_threadpool_signal_and_gui_clipboard(self):
        service = ConversionService()
        results, threads, progress_threads = [], [], []
        service.completed.connect(lambda result: (results.append(result), threads.append(threading.get_ident())))
        service.progress_changed.connect(lambda progress: progress_threads.append(threading.get_ident()))
        service.submit([self.source], "image:jpg")
        pump_until(lambda: len(results) == 1 and service.active_count == 0)
        self.assertFalse(results[0].errors)
        self.assertEqual(threads[0], threading.get_ident())
        self.assertTrue(progress_threads)
        self.assertTrue(all(thread_id == threading.get_ident() for thread_id in progress_threads))
        service.submit([self.source], "copy_path")
        self.assertEqual(APP.clipboard().text(), str(self.source))
        self.assertEqual(results[-1].completed, 1)

    def test_worker_reports_partial_failure(self):
        broken = self.root / "broken.png"
        broken.write_text("not an image")
        worker = ConversionWorker(1, (self.source, broken), ALL_ACTIONS["image:jpg"], None)
        results = []
        worker.signals.finished.connect(results.append)
        with self.assertLogs("magic_converter.core.conversion.converter", level="ERROR"):
            worker.run()
        self.assertEqual(results[0].completed, 1)
        self.assertEqual(len(results[0].errors), 1)

    @unittest.skipUnless(os.name == "nt", "Windows terminal action")
    def test_terminal_uses_cwd_without_interpolating_commands(self):
        special = self.root / "folder with ' quote & spaces"
        special.mkdir()
        paths = (special / "first.txt", special / "second.txt", self.root / "third.txt")
        with patch("magic_converter.core.conversion.converter.shutil.which", return_value="powershell.exe"), \
             patch("magic_converter.core.conversion.converter.subprocess.Popen") as popen:
            open_terminals(paths)
        self.assertEqual(popen.call_count, 2)
        self.assertEqual(popen.call_args_list[0].kwargs["cwd"], special)
        self.assertEqual(popen.call_args_list[0].args[0], ["powershell.exe", "-NoLogo", "-NoProfile", "-NoExit"])
        self.assertEqual(popen.call_args_list[0].kwargs["creationflags"], subprocess.CREATE_NEW_CONSOLE)


class OverlayTests(unittest.TestCase):
    def setUp(self):
        self.wheel = RadialWheelWindow(lambda: False)
        self.wheel.show_for_drag("convert", QPoint(500, 500))
        self.mime = QMimeData()
        self.mime.setUrls([QUrl.fromLocalFile(str(Path("example.png").absolute()))])

    def tearDown(self):
        self.wheel.dismiss()
        self.wheel.deleteLater()
        APP.processEvents()

    def test_drag_drop_forces_copy_even_with_shift(self):
        actions = Qt.DropAction.CopyAction | Qt.DropAction.MoveAction
        enter = QDragEnterEvent(QPoint(160, 60), actions, self.mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        self.wheel.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        self.assertEqual(enter.dropAction(), Qt.DropAction.CopyAction)
        self.assertEqual(len(self.wheel.slots), 9)
        move = QDragMoveEvent(QPoint(160, 60), actions, self.mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        self.wheel.dragMoveEvent(move)
        selected = []
        self.wheel.action_dropped.connect(lambda paths, key: selected.append(key))
        drop = QDropEvent(QPointF(160, 60), actions, self.mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        self.wheel.dropEvent(drop)
        self.assertEqual(selected, ["image:png"])
        self.assertEqual(drop.dropAction(), Qt.DropAction.CopyAction)
        self.assertFalse(self.wheel.isVisible())

    def test_center_remote_urls_and_release_race(self):
        enter = QDragEnterEvent(QPoint(160, 160), Qt.DropAction.CopyAction, self.mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.wheel.dragEnterEvent(enter)
        self.assertEqual(self.wheel.hovered, -1)
        self.wheel.request_dismiss("mouse_released")
        self.assertTrue(self.wheel.isVisible())
        pump_until(lambda: not self.wheel.isVisible())
        self.mime.setUrls([QUrl("https://example.org/a.png")])
        enter = QDragEnterEvent(QPoint(160, 60), Qt.DropAction.CopyAction, self.mime,
                                Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        self.wheel.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        self.assertEqual(len(self.wheel.web_inputs), 1)

    def test_render_wheel_and_settings(self):
        self.wheel.paths = (Path("sample.png"),)
        self.wheel._rebuild_slots()
        self.wheel._set_hover(1)
        self.wheel._open_animation.setCurrentTime(150)
        APP.processEvents()
        pixmap = self.wheel.grab()
        self.assertEqual(pixmap.width() / pixmap.devicePixelRatio(), 320)
        artifact_dir = Path(".test-artifacts")
        artifact_dir.mkdir(exist_ok=True)
        self.assertTrue(pixmap.save(str(artifact_dir / "wheel.png")))
        dialog = SettingsDialog({"enabled": True})
        self.assertEqual(dialog.values()["interval_ms"], 35)
        dialog.deleteLater()
        tray = TrayIcon({"enabled": True})
        tray.show_settings()
        self.assertIsNotNone(tray._settings_dialog)
        tray._settings_dialog.reject()
        self.assertIsNone(tray._settings_dialog)
        tray.deleteLater()

    def test_open_animation_grows_overshoots_and_keeps_drop_target(self):
        self.assertEqual(self.wheel._open_animation.duration(), 150)
        origin = self.wheel.pos()
        self.wheel._open_animation.setCurrentTime(0)
        self.assertEqual(self.wheel.wheelScale, 0)
        self.wheel._open_animation.setCurrentTime(65)
        self.assertGreater(self.wheel.wheelScale, 0)
        self.wheel._open_animation.setCurrentTime(110)
        self.assertGreater(self.wheel.wheelScale, 1)
        self.assertLess(self.wheel.wheelScale * 147, 160)
        self.wheel.show_for_drag("action", QPoint(900, 900))
        self.assertEqual(self.wheel.pos(), origin)
        self.assertGreater(self.wheel.wheelScale, 1)
        self.wheel._open_animation.setCurrentTime(150)
        self.assertAlmostEqual(self.wheel.wheelScale, 1)
        self.wheel.dismiss()
        self.wheel.show_for_drag("convert", QPoint(300, 300))
        self.assertEqual(self.wheel.wheelScale, 0)

    def test_progress_overlay_batch_unknown_and_completion(self):
        overlay = ProgressOverlay()
        overlay.update_progress(JobProgress(1, "MP4", "one.mp4", 2, 2, 50))
        self.assertEqual(overlay.rows[1].bar.value(), 75)
        self.assertTrue(overlay.isVisible())
        overlay.update_progress(JobProgress(2, "GIF", "two.mp4", 1, 1, None))
        self.assertEqual(overlay.rows[2].bar.maximum(), 0)
        result = JobResult(1, ALL_ACTIONS["media:mp4"], 2, completed=2)
        overlay.finish_job(result)
        self.assertEqual(overlay.rows[1].bar.value(), 100)
        two_rows_height = overlay.height()
        overlay._remove(1)
        self.assertLess(overlay.height(), two_rows_height)
        self.assertTrue(overlay.isVisible())
        artifact_dir = Path(".test-artifacts")
        artifact_dir.mkdir(exist_ok=True)
        overlay.update_progress(JobProgress(2, "GIF", "샘플 영상.mp4", 1, 2, 84))
        APP.processEvents()
        overlay.grab().save(str(artifact_dir / "progress.png"))
        overlay.finish_job(JobResult(2, ALL_ACTIONS["media:gif"], 2, errors=["Encoder failed"], completed=1))
        self.assertEqual(overlay.rows[2].bar.format(), "오류")
        overlay._remove(2)
        self.assertFalse(overlay.isVisible())
        overlay.shutdown()
        overlay.deleteLater()

    def test_cleanup_does_not_request_quit_again_and_is_idempotent(self):
        from magic_converter.main import ApplicationManager

        with tempfile.TemporaryDirectory() as directory, \
             patch.object(QSystemTrayIcon, "show"), patch.object(APP, "quit") as quit_app:
            manager = ApplicationManager(APP, data_dir=Path(directory), isolated=True)
            manager.hook.set_enabled(False)
            self.assertIsNotNone(manager.watch)
            manager.exiting = True
            with patch.object(manager.watch, "stop", wraps=manager.watch.stop) as stop:
                manager._cleanup()
                manager._cleanup()
                manager.request_quit()
                quit_app.assert_not_called()
                stop.assert_called_once()
            manager.wheel.deleteLater()
            manager.progress_overlay.deleteLater()
            manager.deleteLater()
            APP.processEvents()

    def test_manager_applies_settings_and_waits_for_jobs_on_exit(self):
        from PySide6.QtCore import QSettings
        from magic_converter.main import ApplicationManager

        with tempfile.TemporaryDirectory() as directory:
            settings = QSettings(str(Path(directory) / "settings.ini"), QSettings.Format.IniFormat)
            idle_reader = lambda: InputState(False, False, False, 0, 0)
            with patch("magic_converter.main.QSettings", return_value=settings), \
                 patch("magic_converter.main.HookManager", side_effect=lambda parent, **kwargs: HookManager(parent, reader=idle_reader, **kwargs)), \
                 patch.object(QSystemTrayIcon, "show"), patch.object(APP, "quit") as quit_app:
                manager = ApplicationManager(APP)
                manager.show_startup(tray_only=True)
                self.assertIsNone(manager.tray._settings_dialog)
                manager.show_startup()
                self.assertTrue(manager.tray._settings_dialog.isVisible())
                self.assertIn("실행 중", manager.tray._settings_dialog.running_label.text())
                manager.tray._settings_dialog.reject()
                self.assertIsNone(manager.tray._settings_dialog)
                manager._apply_settings({"enabled": True, "interval_ms": 40, "threshold": 20, "ffmpeg_path": ""})
                self.assertTrue(manager.hook.enabled)
                self.assertEqual(manager.hook.timer.interval(), 40)
                self.assertEqual(settings.value("threshold", type=int), 20)
                source = Path(directory) / "sample.png"
                Image.new("RGB", (128, 128), "orange").save(source)
                manager.service.submit([source], "image:jpg")
                manager.request_quit()
                self.assertFalse(manager.hook.enabled)
                self.assertFalse(quit_app.called)
                pump_until(lambda: quit_app.called)
                self.assertTrue((source.parent / "sample_converted.jpg").exists())
                manager._cleanup()
                manager.wheel.deleteLater()
                manager.progress_overlay.deleteLater()
                manager.deleteLater()
                APP.processEvents()


@unittest.skipUnless(find_ffmpeg(), "FFmpeg not available")
class FFmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.ffmpeg = find_ffmpeg()
        cls.source = cls.root / "sample.mp4"
        cls.audio = cls.root / "sound.wav"
        common = [cls.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y"]
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        subprocess.run(common + ["-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=0.4",
                                 "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", "-c:v", "libx264",
                                 "-c:a", "aac", "-metadata", "title=PRIVATE_TITLE", "-shortest", str(cls.source)],
                       check=True, capture_output=True, creationflags=flags)
        subprocess.run(common + ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", str(cls.audio)],
                       check=True, capture_output=True, creationflags=flags)

    @classmethod
    def tearDownClass(cls):
        cls.temp.cleanup()

    def test_real_video_and_audio_encoders(self):
        original = hashlib.sha256(self.source.read_bytes()).hexdigest()
        for key, suffix in (("media:mp4", ".mp4"), ("media:gif", ".gif"),
                            ("media:mp3", ".mp3"), ("media:webm", ".webm"), ("strip", ".mp4")):
            with self.subTest(key=key):
                output = self.root / (key.replace(":", "_") + suffix)
                convert_media(self.source, output, key, self.ffmpeg)
                self.assertGreater(output.stat().st_size, 0)
                if key == "strip":
                    self.assertNotIn(b"PRIVATE_TITLE", output.read_bytes())
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), original)
        for key, suffix in (("media:mp4", ".mp4"), ("media:webm", ".webm"), ("media:mp3", ".mp3")):
            with self.subTest(audio=key):
                output = self.root / ("audio_" + key.replace(":", "_") + suffix)
                convert_media(self.audio, output, key, self.ffmpeg)
                self.assertGreater(output.stat().st_size, 0)

    def test_actual_progress_uses_duration_and_completes_after_success(self):
        paced_source = self.root / "paced-source.mp4"
        subprocess.run([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                        "-stream_loop", "5", "-i", str(self.source), "-c", "copy", str(paced_source)],
                       check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.assertGreater(probe_duration(paced_source, self.ffmpeg), 2)
        updates = []
        output = self.root / "progress.mp4"
        # Pace input and disable x264 frame buffering only for this test,
        # ensuring multiple progress packets arrive before process exit.
        from magic_converter.core.conversion.converter import media_command

        def streaming_command(*args):
            command = media_command(*args)
            command.insert(command.index("-i"), "-re")
            command[-1:-1] = ["-tune", "zerolatency"]
            return command

        with patch("magic_converter.core.conversion.converter.media_command", side_effect=streaming_command):
            convert_media(paced_source, output, "media:mp4", self.ffmpeg, updates.append)
        self.assertIsNone(updates[0])
        numbers = [value for value in updates if value is not None]
        self.assertEqual(numbers, sorted(numbers))
        self.assertEqual(numbers[0], 0)
        self.assertEqual(numbers[-1], 100)
        self.assertTrue(any(0 < number < 99 for number in numbers), numbers)

    def test_ffmpeg_failure_removes_partial_file(self):
        broken = self.root / "broken.mp4"
        broken.write_bytes(b"not a video")
        updates = []
        with self.assertRaises(ConversionError):
            with output_file(broken, ".mp4") as (temporary, destination):
                convert_media(broken, temporary, "media:mp4", self.ffmpeg, updates.append)
        self.assertFalse(destination.exists())
        self.assertNotIn(100, updates)


if __name__ == "__main__":
    unittest.main()
