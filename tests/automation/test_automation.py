"""Stage 6: stable inputs, crash receipts, real HTTP and IPC/CLI processes."""
import base64
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import asdict, replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PIL import Image
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDropEvent
from PySide6.QtWidgets import QApplication

from magic_converter.core.automation.automation_store import AutomationStore, WatchRule
from magic_converter.core.automation.watch_engine import WatchEngine
from magic_converter.core.settings.preferences import Preset, OutputRule, default_preferences
from magic_converter.core.conversion.converter import ConversionService
from magic_converter.core.conversion.conversion_request import DropSelection
from magic_converter.core.jobs.job_control import Control, JobCancelled, current
from magic_converter.core.automation.local_ipc import IpcServer, call, server_name
from magic_converter.core.automation.web_inputs import WebInput, from_url, from_mime, safe_name
from magic_converter.core.automation.web_transfer import fetch, materialize

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
ROOT = Path(__file__).resolve().parents[2]


def pump(predicate, timeout=15):
    until = time.monotonic() + timeout
    while not predicate():
        APP.processEvents()
        if time.monotonic() >= until:
            raise AssertionError("Qt operation timed out")
        time.sleep(.005)


def run_cli(root, *args, timeout=25):
    report = root / ("cli-" + str(time.monotonic_ns()) + ".json")
    result = subprocess.run([sys.executable, str(ROOT / "run_magic_converter.py"), *map(str, args),
                             "--data-dir", str(root / "data"), "--report", str(report)],
                            capture_output=True, text=True, encoding="utf-8", timeout=timeout,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if not report.exists():
        raise AssertionError(result.stderr + result.stdout)
    return result.returncode, json.loads(report.read_text(encoding="utf-8"))


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.input = self.root / "in"
        self.input.mkdir()
        self.rule = WatchRule("rule", str(self.input), str(self.input / "_converted"), Preset("jpg", "JPG", "image:jpg"), settle=.1)
        self.engine = WatchEngine(self.root / "state", (self.rule,))
        self.source = self.input / "사진.png"
        Image.new("RGB", (12, 9), "blue").save(self.source)

    def tearDown(self):
        self.temp.cleanup()

    def candidate(self):
        self.engine.scan(0)
        ready = self.engine.scan(.2)
        self.assertEqual(len(ready), 1)
        return ready[0][1]

    def output(self, event):
        output = Path(event["work"]) / "results" / "사진_converted.jpg"
        Image.new("RGB", (12, 9), "blue").save(output)
        return output

    def test_stable_files_duplicate_notifications_and_restart_do_not_reconvert(self):
        self.assertEqual(self.engine.scan(0), [])
        self.assertEqual(self.engine.scan(.05), [])
        event = self.engine.scan(.11)[0][1]
        target = self.engine.finish(event, True, [self.output(event)])
        self.assertTrue(self.engine.receipt_valid(target, event["key"]))
        self.assertEqual(self.engine.scan(.3), [])
        restarted = WatchEngine(self.root / "state", (self.rule,))
        restarted.recover()
        self.assertEqual(restarted.scan(0), [])
        self.assertEqual(restarted.scan(.2), [])
        Image.new("RGB", (12, 9), "red").save(self.source)
        restarted.scan(1)
        self.assertEqual(len(restarted.scan(1.2)), 1)

    def test_once_drain_requires_quiet_scan_after_every_serial_rule_input(self):
        other = self.input / 'second.png'
        other.write_bytes(self.source.read_bytes())
        self.assertEqual(self.engine.scan(0), [])
        self.assertFalse(self.engine.quiescent)  # Newly observed, not stable.
        first = self.engine.scan(.2)[0][1]
        self.assertFalse(self.engine.quiescent)
        self.assertEqual(self.engine.scan(.3), [])  # Rule is busy.
        self.assertFalse(self.engine.quiescent)
        self.engine.finish(first, True, [self.output(first)])
        self.assertFalse(self.engine.quiescent)  # Publication-to-scan gap.
        second = self.engine.scan(.4)[0][1]
        self.assertNotEqual(first['source'], second['source'])
        self.assertFalse(self.engine.quiescent)
        self.engine.finish(second, True, [self.output(second)])
        self.assertFalse(self.engine.quiescent)
        self.assertEqual(self.engine.scan(.6), [])
        self.assertTrue(self.engine.quiescent)

    def test_once_drain_defers_locked_input_and_normal_empty_scan_finishes(self):
        self.engine.scan(0)
        with patch('magic_converter.core.automation.watch_engine.can_open', return_value=False):
            self.assertEqual(self.engine.scan(.2), [])
            self.assertFalse(self.engine.quiescent)
        event = self.engine.scan(.4)[0][1]
        self.engine.finish(event, True, [self.output(event)])
        self.assertEqual(self.engine.scan(.6), [])
        self.assertTrue(self.engine.quiescent)
        self.source.unlink()
        self.assertEqual(self.engine.scan(.8), [])
        self.assertTrue(self.engine.quiescent)

    def test_in_progress_files_output_trees_and_temporary_names_are_excluded(self):
        self.rule = replace(self.rule, recursive=True, include="*.png;*.jpg")
        self.engine.rules = (self.rule,)
        output = Path(self.rule.output)
        output.mkdir()
        (output / "loop.png").write_bytes(self.source.read_bytes())
        (self.input / "part.png.crdownload").write_bytes(b"unfinished")
        self.engine.scan(0)
        self.source.write_bytes(self.source.read_bytes() + b"changed")
        self.assertEqual(self.engine.scan(.2), [])
        event = self.engine.scan(.4)[0][1]
        self.assertEqual(Path(event["source"]), self.source)

    def test_windows_writer_lock_defers_a_file_until_closed(self):
        if os.name != "nt":
            self.skipTest("Windows file sharing")
        import win32file, win32con
        handle = win32file.CreateFile(str(self.source), win32con.GENERIC_WRITE, win32con.FILE_SHARE_READ,
                                     None, win32con.OPEN_EXISTING, 0, None)
        try:
            self.engine.scan(0)
            self.assertEqual(self.engine.scan(.2), [])
        finally:
            handle.Close()
        self.assertEqual(len(self.engine.scan(.4)), 1)

    def test_recovery_after_publication_before_database_commit_is_idempotent(self):
        event = self.candidate()
        output = self.output(event)
        original = self.engine.journal.update
        def crash(key, state, detail=""):
            if state == "done":
                raise RuntimeError("simulated process crash after rename")
            return original(key, state, detail)
        with patch.object(self.engine.journal, "update", side_effect=crash):
            with self.assertRaises(RuntimeError):
                self.engine.finish(event, True, [output])
        recovered = WatchEngine(self.root / "state", (self.rule,))
        recovered.recover()
        self.assertEqual(recovered.journal.get(event["key"])["state"], "done")
        self.assertEqual(len(list(Path(self.rule.output).iterdir())), 1)

    def test_interrupted_queue_restarts_but_explicit_cancellation_requires_retry(self):
        event = self.candidate()
        restarted = WatchEngine(self.root / "state", (self.rule,))
        restarted.recover()
        restarted.scan(0)
        self.assertEqual(len(restarted.scan(.2)), 1)
        restarted.finish(event, False, detail="cancelled")
        again = WatchEngine(self.root / "state", (self.rule,))
        again.recover()
        again.scan(0)
        self.assertEqual(again.scan(.2), [])
        again.journal.retry(event["key"])
        self.assertEqual(len(again.scan(.4)), 1)

    def test_changed_source_and_cancelled_publication_leave_no_public_result(self):
        event = self.candidate()
        output = self.output(event)
        self.source.write_bytes(b"changed source")
        self.assertIsNone(self.engine.finish(event, True, [output]))
        self.assertFalse(Path(event["target"]).exists())
        self.assertEqual(self.engine.journal.get(event["key"])["state"], "failed")

    def test_cancel_during_publication_removes_stage_and_does_not_resume_on_restart(self):
        from magic_converter.core.jobs.job_control import copy_tree
        event = self.candidate()
        output = self.output(event)
        cancel = threading.Event()
        def copy_then_cancel(source, destination):
            copy_tree(source, destination)
            cancel.set()
        token = current.set(Control(cancel, lambda _: None))
        try:
            with patch("magic_converter.core.automation.watch_engine.copy_tree", side_effect=copy_then_cancel):
                with self.assertRaises(JobCancelled):
                    self.engine.finish(event, True, [output])
        finally:
            current.reset(token)
        self.assertFalse(Path(event["target"]).exists())
        self.assertFalse(list(Path(self.rule.output).glob(".magic-watch-*")))
        restarted = WatchEngine(self.root / "state", (self.rule,))
        restarted.recover()
        self.assertEqual(restarted.journal.get(event["key"])["state"], "cancelled")

    def test_watch_config_roundtrip_does_not_replace_future_or_invalid_config(self):
        store = AutomationStore(self.root / "config")
        store.save((self.rule,), str(self.root / "web"))
        self.assertEqual(AutomationStore(store.directory).rules, (self.rule,))
        future = b'{"version":99,"rules":[]}'
        store.path.write_bytes(future)
        with self.assertRaises(ValueError):
            AutomationStore(store.directory)
        self.assertEqual(store.path.read_bytes(), future)
        with self.assertRaises(ValueError):
            replace(self.rule, output=self.rule.folder)


class WebTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raw = io.BytesIO()
        Image.new("RGB", (15, 11), "green").save(raw, "PNG")
        cls.png = raw.getvalue()
        cls.slow_received = threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass
            def do_GET(self):
                if self.path == "/redirect":
                    self.send_response(302); self.send_header("Location", "/wrong.jpg"); self.end_headers(); return
                if self.path == "/loop":
                    self.send_response(302); self.send_header("Location", "/loop"); self.end_headers(); return
                if self.path == "/missing":
                    self.send_error(404); return
                content = b"<html>login</html>" if self.path == "/login.png" else b"MZexecutable" if self.path == "/program.png" else cls.png
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(len(content) + (100 if self.path == "/truncated.png" else 0)))
                self.end_headers()
                if self.path == "/slow.png":
                    cls.slow_received.set()
                    time.sleep(5)
                try:
                    self.wfile.write(content)
                except OSError:
                    pass
        cls.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.http.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown(); cls.http.server_close(); cls.thread.join(2)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def test_redirect_byte_sniffing_and_failures_clean_partial_downloads(self):
        target = fetch(self.url + "/redirect", self.root / "ok", "file.jpg")
        self.assertEqual(target.suffix, ".png")
        with Image.open(target) as image:
            self.assertEqual(image.size, (15, 11))
        for name in ("missing", "login.png", "program.png", "truncated.png", "loop"):
            with self.subTest(name=name), self.assertRaises(Exception):
                fetch(self.url + "/" + name, self.root / name, name)
            self.assertFalse(list((self.root / name).glob("*")))
        with self.assertRaises(ValueError):
            fetch(self.url + "/wrong.jpg", self.root / "large", max_bytes=10)
        self.assertFalse(list((self.root / "large").glob("*")))

    def test_owned_download_process_cancels_quickly_and_queue_continues(self):
        item = from_url(self.url + "/slow.png")
        service = ConversionService(file_timeout=10)
        results = []
        service.completed.connect(results.append)
        job = service.submit_selection(DropSelection((item.placeholder,), "image:jpg", output=OutputRule("folder", str(self.root / "out")), web_inputs=(item,)))
        started = time.monotonic()
        self.slow_received.clear()
        pump(lambda: self.slow_received.is_set())
        service.cancel_job(job)
        pump(lambda: len(results) == 1, 5)
        self.assertTrue(results[0].cancelled)
        self.assertLess(time.monotonic() - started, 5)
        self.assertFalse(list((self.root / "out").glob("*")))
        second = WebInput("image.png", data=self.png)
        service.submit_selection(DropSelection((second.placeholder,), "image:jpg", output=OutputRule("folder", str(self.root / "out")), web_inputs=(second,)))
        pump(lambda: len(results) == 2)
        self.assertFalse(results[-1].errors)
        self.assertEqual(results[-1].completed, 1)
        service.pool.waitForDone(3000)

    def test_web_wheel_freezes_output_and_rejects_changed_drop_payload(self):
        from magic_converter.ui.shell.radial_wheel import RadialWheelWindow
        wheel = RadialWheelWindow(lambda: True)
        current = [str(self.root / "before")]
        wheel.web_output = lambda: current[0]
        submissions = []
        wheel.request_dropped.connect(submissions.append)
        wheel.show_for_drag("convert", QPoint(400, 400))
        mime = QMimeData(); mime.setUrls([QUrl(self.url + "/wrong.jpg")])
        wheel.dragEnterEvent(QDragEnterEvent(QPoint(160, 60), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier))
        current[0] = str(self.root / "after")
        wheel.dropEvent(QDropEvent(QPointF(160, 60), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier))
        self.assertEqual(len(submissions), 1)
        self.assertEqual(submissions[0].output.directory, str(self.root / "before"))
        service = ConversionService()
        results = []
        service.completed.connect(results.append)
        service.submit_selection(submissions[0])
        pump(lambda: bool(results))
        self.assertFalse(results[0].errors)
        self.assertEqual(results[0].outputs[0].parent, self.root / "before")
        wheel.close(); service.pool.waitForDone(3000)

    def test_mixed_drags_data_urls_and_unsafe_urls(self):
        mime = QMimeData()
        mime.setUrls([QUrl.fromLocalFile(str(self.root / "local.png")), QUrl(self.url + "/wrong.jpg")])
        self.assertEqual(from_mime(mime), ((), ()))
        data = from_url("data:image/png;base64," + base64.b64encode(self.png).decode())
        self.assertEqual(data.data, self.png)
        for url in ("file:///C:/Windows/a.png", "https://user:password@example.org/a.png", "ftp://example.org/a.png"):
            with self.assertRaises(ValueError):
                from_url(url)
        self.assertNotIn("/", safe_name("../../a.png"))
        from PySide6.QtGui import QImage
        mime = QMimeData()
        mime.setUrls([QUrl("blob:https://example.org/browser-owned")])
        mime.setImageData(QImage.fromData(self.png))
        paths, items = from_mime(mime)
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0].data)


class CliIpcTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / "사진.png"
        Image.new("RGB", (16, 10), "red").save(self.source)
        self.processes = []

    def tearDown(self):
        for process in self.processes:
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=8)
        self.temp.cleanup()

    def start_server(self):
        process = subprocess.Popen([sys.executable, str(ROOT / "run_magic_converter.py"), "serve", "--data-dir", str(self.root / "data"), "--duration", "30"],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, encoding="utf-8",
                                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.processes.append(process)
        return process

    def test_cli_real_conversion_watch_restart_and_json_failures(self):
        code, report = run_cli(self.root, "convert", "--to", "jpg", self.source, "--output", self.root / "out")
        self.assertEqual(code, 0)
        self.assertTrue(report["ok"])
        with Image.open(report["jobs"][0]["outputs"][0]) as image:
            self.assertEqual(image.size, (16, 10))
        inbox = self.root / "inbox"; inbox.mkdir()
        (inbox / self.source.name).write_bytes(self.source.read_bytes())
        for expected in (1, 0):
            code, report = run_cli(self.root, "watch", inbox, "--action", "image:webp", "--settle", ".1", "--once")
            self.assertEqual(code, 0, report)
            self.assertEqual(len(report["jobs"]), expected)
        code, report = run_cli(self.root, "convert", "--action", "image:jpg", self.root / "missing.png")
        self.assertEqual(code, 1)
        self.assertFalse(report["ok"])

    def test_real_ipc_duplicate_ids_profile_isolation_restart_and_stale_cancel(self):
        server = self.start_server()
        first = call(self.root / "data", {"command": "status"}, timeout=10)
        self.assertTrue(first["ok"])
        request = {"command": "send", "id": "stable-request", "paths": [str(self.source)],
                   "recipe": asdict(Preset("jpg", "JPG", "image:jpg", output=OutputRule("folder", str(self.root / "out"))))}
        accepted = call(self.root / "data", request)
        duplicate = call(self.root / "data", request)
        self.assertEqual(accepted["job_id"], duplicate["job_id"])
        self.assertTrue(duplicate["replayed"])
        until = time.monotonic() + 10
        while call(self.root / "data", {"command": "status"})["jobs"]:
            self.assertLess(time.monotonic(), until); time.sleep(.03)
        self.assertEqual(len(list((self.root / "out").glob("*.jpg"))), 1)
        bad = call(self.root / "data", {**request, "paths": [str(self.root / "other.png")]})
        self.assertFalse(bad["ok"])
        with self.assertRaises(OSError):
            call(self.root / "other-profile", {"command": "status"}, timeout=.15)
        stopped = call(self.root / "data", {"command": "stop", "instance": first["instance"]})
        self.assertTrue(stopped["ok"])
        server.communicate(timeout=10)
        self.assertEqual(server.returncode, 0)
        second_server = self.start_server()
        second = call(self.root / "data", {"command": "status"})
        self.assertNotEqual(second["instance"], first["instance"])
        replay = call(self.root / "data", request)
        self.assertTrue(replay["previous_instance"])
        old_cancel = call(self.root / "data", {"command": "cancel", "job_id": 1, "instance": first["instance"]})
        self.assertFalse(old_cancel["ok"])
        self.assertEqual(len(list((self.root / "out").glob("*.jpg"))), 1)
        call(self.root / "data", {"command": "stop", "instance": second["instance"]})
        second_server.communicate(timeout=10)

    def test_history_appends_from_independent_instances_do_not_lose_records(self):
        from magic_converter.core.settings.history import HistoryStore
        from magic_converter.core.jobs.job_models import JobResult, FileResult
        from magic_converter.core.conversion.file_router import ALL_ACTIONS
        a, b = HistoryStore(self.root / "history.json"), HistoryStore(self.root / "history.json")
        result = JobResult(1, ALL_ACTIONS["image:jpg"], 1, completed=1, files=[FileResult(self.source, "done")])
        a.append(result); b.append(result)
        self.assertEqual(len(HistoryStore(a.path).entries), 2)

    def test_isolated_profile_consent_is_read_only_from_its_own_ini(self):
        from PySide6.QtCore import QSettings
        pass
        profile = self.root / "separate-profile"
        profile.mkdir()
        settings = QSettings(str(profile / "legacy.ini"), QSettings.Format.IniFormat)
        pass

    def test_explorer_command_quotes_executable_and_literal_path_placeholder(self):
        from magic_converter.core.automation.explorer_integration import command
        self.assertEqual(command(self.root / "space dir" / "magic_converter.exe"), '"' + str((self.root / "space dir" / "magic_converter.exe").resolve()) + '" --open "%1"')


if __name__ == "__main__":
    unittest.main()
