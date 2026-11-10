"""Real silent/flooding processes and cancellation-safe output publication."""

import ctypes
import io
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from magic_converter.core.jobs.job_control import (Control, JobCancelled, JobTimedOut, CancellableRLock,
                                               check_cancelled, copy_stream, current)
from magic_converter.core.jobs.process_runner import run_process


class ProcessRunnerTests(unittest.TestCase):
    def test_cancel_interrupts_a_silent_child_without_waiting_for_stdout(self):
        event = threading.Event()
        token = current.set(Control(event, lambda _: None))
        timer = threading.Timer(0.2, event.set)
        started = time.monotonic()
        timer.start()
        try:
            with self.assertRaises(JobCancelled):
                run_process([sys.executable, "-c", "import time;time.sleep(30)"])
        finally:
            timer.cancel()
            current.reset(token)
        self.assertLess(time.monotonic() - started, 3)

    def test_idle_and_absolute_timeouts_stop_a_real_process(self):
        for options, program in (({"idle_timeout": 0.25}, "import time;time.sleep(30)"),
                                  ({"timeout": 0.35}, "import time\nwhile True:\n print('alive',flush=True)\n time.sleep(.02)")):
            with self.subTest(options=options):
                started = time.monotonic()
                with self.assertRaises(JobTimedOut):
                    run_process([sys.executable, "-c", program], **options)
                self.assertLess(time.monotonic() - started, 3)

    def test_the_files_deadline_applies_even_without_a_command_timeout(self):
        token = current.set(Control(threading.Event(), lambda _: None, time.monotonic() + 0.25))
        try:
            with self.assertRaises(JobTimedOut):
                run_process([sys.executable, "-c", "import time;time.sleep(30)"])
        finally:
            current.reset(token)

    def test_large_stderr_cannot_block_progress_and_captured_logs_are_bounded(self):
        lines = []
        program = "import sys\nprint('out_time_us=1000',flush=True)\nsys.stderr.write('x'*1000000)\nprint('progress=end',flush=True)"
        result = run_process([sys.executable, "-c", program], timeout=5, on_output=lines.append, capture_limit=2048)
        self.assertEqual(result.returncode, 0)
        self.assertEqual(lines, ["out_time_us=1000", "progress=end"])
        self.assertEqual(len(result.stderr), 2048)

    def test_engine_logs_have_a_disk_limit(self):
        with self.assertRaisesRegex(RuntimeError, "로그 크기"):
            run_process([sys.executable, "-c", "import sys;sys.stderr.write('x'*1000000)"],
                        timeout=5, log_limit=1024)

    def test_timeout_before_publication_removes_the_reserved_name_and_partial_output(self):
        from magic_converter.core.conversion.converter import output_file
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.txt"
            source.write_text("original", encoding="utf-8")
            control = Control(threading.Event(), lambda _: None)
            token = current.set(control)
            try:
                with self.assertRaises(JobTimedOut):
                    with output_file(source, ".md") as (temporary, _):
                        temporary.write_text("partial", encoding="utf-8")
                        control.deadline = time.monotonic() - 1
            finally:
                current.reset(token)
            self.assertEqual(list(Path(directory).iterdir()), [source])
            self.assertEqual(source.read_text(), "original")

    def test_media_cancellation_stops_a_silent_encoder_and_removes_partial_output(self):
        from magic_converter.core.conversion.converter import convert_media, output_file
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source.mp4"
            source.write_bytes(b"original")
            event = threading.Event()
            token = current.set(Control(event, lambda _: None))
            timer = threading.Timer(0.25, event.set)

            def command(ffmpeg, incoming, outgoing, key):
                return [sys.executable, "-c", "import sys,time;from pathlib import Path;Path(sys.argv[1]).write_bytes(b'partial');time.sleep(30)", str(outgoing)]

            timer.start()
            try:
                with patch("magic_converter.core.conversion.converter.probe_duration", return_value=None), \
                        patch("magic_converter.core.conversion.converter.media_command", side_effect=command), self.assertRaises(JobCancelled):
                    with output_file(source, ".mp4") as (temporary, _):
                        convert_media(source, temporary, "media:mp4", sys.executable)
            finally:
                timer.cancel()
                current.reset(token)
            self.assertEqual(list(Path(directory).iterdir()), [source])
            self.assertEqual(source.read_bytes(), b"original")

    @unittest.skipUnless(os.name == "nt", "Windows job-object lifetime")
    def test_an_exited_parent_cannot_leave_an_inheriting_child_running(self):
        with tempfile.TemporaryDirectory() as directory:
            child_file = Path(directory) / "child.txt"
            program = ("import subprocess,sys,time\nfrom pathlib import Path\n"
                       "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])\n"
                       "Path(sys.argv[1]).write_text(str(p.pid))\n")
            result = run_process([sys.executable, "-c", program, str(child_file)], timeout=5)
            self.assertEqual(result.returncode, 0)
            pid = int(child_file.read_text())
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
            kernel.OpenProcess.restype = ctypes.c_void_p
            kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
            kernel.CloseHandle.argtypes = [ctypes.c_void_p]
            handle = kernel.OpenProcess(0x1000, False, pid)
            if handle:
                try:
                    code = ctypes.c_ulong()
                    self.assertTrue(kernel.GetExitCodeProcess(handle, ctypes.byref(code)))
                    self.assertNotEqual(code.value, 259, "Owned child is still running")
                finally:
                    kernel.CloseHandle(handle)

    def test_cancellation_during_copy_is_observed_between_chunks(self):
        event = threading.Event()

        class CancellingSource(io.BytesIO):
            def read(self, size=-1):
                chunk = super().read(size)
                event.set()
                return chunk

        token = current.set(Control(event, lambda _: None))
        destination = io.BytesIO()
        try:
            with self.assertRaises(JobCancelled):
                copy_stream(CancellingSource(b"abcdef"), destination, chunk_size=2)
        finally:
            current.reset(token)
        self.assertLessEqual(len(destination.getvalue()), 2)

    def test_waiting_for_a_renderer_lock_is_cancellable(self):
        lock, acquired, release = CancellableRLock(), threading.Event(), threading.Event()

        def holder():
            with lock:
                acquired.set()
                release.wait(3)

        thread = threading.Thread(target=holder)
        thread.start()
        self.assertTrue(acquired.wait(1))
        token = current.set(Control(threading.Event(), lambda _: None, time.monotonic() + 0.2))
        try:
            with self.assertRaises(JobTimedOut):
                with lock:
                    self.fail("A held renderer lock was entered")
        finally:
            current.reset(token)
            release.set()
            thread.join(3)
        with lock:
            check_cancelled()


if __name__ == "__main__":
    unittest.main()
