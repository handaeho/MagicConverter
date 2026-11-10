"""B06: real non-cooperative library calls, owned children and worker limits."""

import ctypes
import json
import os
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_execution import execute_file
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs import specialized_worker as worker
from magic_converter.core.jobs.job_control import Control, current, JobCancelled, JobTimedOut
from magic_converter.core.jobs.process_runner import run_process


def alive(pid):
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except ProcessLookupError:
            return False
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel.OpenProcess.restype = ctypes.c_void_p
    kernel.GetExitCodeProcess.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    handle = kernel.OpenProcess(0x1000, False, pid)
    if not handle:
        return False
    try:
        code = ctypes.c_ulong()
        return bool(kernel.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
    finally:
        kernel.CloseHandle(handle)


class ParserWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "input.json"
        self.source.write_text('{"x":42}', encoding="utf-8")
        self.request = prepare_request([self.source], "data:yaml", create_catalog())

    def convert(self):
        return execute_file(self.request, self.request.files[0], None, lambda _: None, lambda _: None)

    def test_valid_worker_and_failure_leave_the_next_file_usable(self):
        output = self.convert()
        self.assertIn("42", output.read_text(encoding="utf-8"))
        output.unlink()
        self.source.write_text('{"x":', encoding="utf-8")
        with self.assertRaises(ConversionError):
            self.convert()
        self.assertEqual(list(self.root.iterdir()), [self.source])
        self.source.write_text('{"x":42}', encoding="utf-8")
        self.assertTrue(self.convert().is_file())

    def test_explicit_no_deadline_does_not_gain_a_one_hour_worker_timeout(self):
        import itertools
        token = current.set(Control(threading.Event(), lambda _: None, None))
        clock = itertools.chain([0.0], itertools.repeat(3601.0))
        try:
            with patch("magic_converter.core.jobs.process_runner.time.monotonic", side_effect=clock):
                output = self.convert()
        finally:
            current.reset(token)
        self.assertIn("42", output.read_text(encoding="utf-8"))

    def test_windowed_helper_delivers_notices_with_stdout_unavailable(self):
        source = self.root / "table.csv"
        source.write_text("name,value\nrow,42\n", encoding="utf-8")
        request, report = self.root / "request.json", self.root / "report.json"
        request.write_text(json.dumps({"run_id": "windowed-test", "engine": "data", "source": str(source),
                           "source_kind": "csv", "target_kind": "json", "destination": str(self.root / "result.json")}), encoding="utf-8")
        with patch.object(sys, "stdout", None):
            self.assertEqual(worker.helper_main(request, report), 0)
        result = json.loads(report.read_text(encoding="utf-8"))
        events = [json.loads(line) for line in (self.root / "events.jsonl").read_text(encoding="utf-8").splitlines()]
        self.assertEqual(result["status"], "passed")
        self.assertTrue(any(e["kind"] == "notice" and e["run_id"] == "windowed-test" for e in events))

    def test_cancel_and_deadline_interrupt_a_library_without_checkpoints_and_kill_descendants(self):
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                ready = self.root / "ready.json"
                ready.unlink(missing_ok=True)
                program = """
import json,os,subprocess,sys,time
from pathlib import Path
from magic_converter.core.specialized import data_converter
from magic_converter.core.jobs.specialized_worker import helper_main
def blocked(source,destination,*args):
    destination.write_bytes(b'partial')
    child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)'])
    marker=Path(sys.argv[3]+'.tmp')
    marker.write_text(json.dumps({'worker':os.getpid(),'child':child.pid}))
    marker.replace(sys.argv[3])
    time.sleep(30)
data_converter.convert=blocked
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
"""
                event = threading.Event()
                control = Control(event, lambda _: None)
                token = current.set(control)
                clocks = {}

                def interrupt():
                    deadline = time.monotonic() + 15
                    while not ready.is_file() and time.monotonic() < deadline:
                        time.sleep(.01)
                    clocks["requested"] = time.monotonic()
                    if timeout:
                        control.deadline = time.monotonic() + .05
                    else:
                        event.set()

                signal = threading.Thread(target=interrupt)
                signal.start()
                try:
                    with patch.object(worker, "worker_command", side_effect=lambda a, b: [sys.executable, "-c", program, str(a), str(b), str(ready)]), self.assertRaises(JobTimedOut if timeout else JobCancelled):
                        self.convert()
                finally:
                    signal.join(16)
                    current.reset(token)
                self.assertTrue(ready.is_file(), "Cancellation never reached the actual parser")
                self.assertLess(time.monotonic() - clocks["requested"], 3)
                pids = json.loads(ready.read_text())
                self.assertFalse(alive(pids["worker"]))
                self.assertFalse(alive(pids["child"]))
                ready.unlink()
                self.assertEqual(list(self.root.iterdir()), [self.source])
                self.assertEqual(self.source.read_text(), '{"x":42}')

    @unittest.skipUnless(os.name == "nt", "Windows job memory limits")
    def test_memory_budget_allows_normal_allocation_and_refuses_excess(self):
        program = """
import sys
try:
    data=bytearray(int(sys.argv[1]))
    print('allocated')
except MemoryError:
    print('memory-refused')
"""
        for size, expected in ((1024 * 1024, "allocated"), (256 * 1024 * 1024, "memory-refused")):
            result = run_process([sys.executable, "-c", program, str(size)], timeout=10, memory_limit=96 * 1024 * 1024)
            self.assertEqual(result.returncode, 0)
            self.assertEqual(result.stdout.strip(), expected)

    def test_stale_report_and_crashed_worker_never_publish(self):
        for body in ("os._exit(7)", "Path(sys.argv[2]).write_text('{\"status\":\"passed\",\"run_id\":\"old\"}')"):
            program = "import os,sys;from pathlib import Path;" + body
            with self.subTest(body=body), patch.object(worker, "worker_command", side_effect=lambda a, b: [sys.executable, "-c", program, str(a), str(b)]), self.assertRaises(ConversionError):
                self.convert()
            self.assertEqual(list(self.root.iterdir()), [self.source])

    def test_output_budget_stops_a_noncooperative_writer_before_publication(self):
        program = """
import json,sys,time
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text())
Path(r['destination']).write_bytes(b'x'*1000000)
time.sleep(30)
"""
        started = time.monotonic()
        with patch.object(worker, "WORKER_OUTPUT_BYTES", 1024), patch.object(worker, "worker_command", side_effect=lambda a, b: [sys.executable, "-c", program, str(a), str(b)]), self.assertRaisesRegex(ConversionError, "용량"):
            self.convert()
        self.assertLess(time.monotonic() - started, 3)
        self.assertEqual(list(self.root.iterdir()), [self.source])


if __name__ == "__main__":
    unittest.main()
