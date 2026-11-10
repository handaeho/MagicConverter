"""Office document libraries are killable while COM ownership stays in the parent."""

import json
import hashlib
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import uuid
import zipfile
from pathlib import Path
from unittest.mock import patch

from docx import Document
from pypdf import PdfReader, PdfWriter
from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_execution import execute_file
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.engines.office_process import run_helper
from magic_converter.core.jobs import specialized_worker as worker
from magic_converter.core.jobs.job_control import Control, current, JobCancelled, JobTimedOut
from magic_converter.core.jobs.office_broker import handle_request, native_proxy, write_json
from tests.jobs.test_parser_workers import alive

BOOTSTRAP = '''
import sys
from magic_converter.core.engines import hwp_converter as h,office_engine,rhwp_engine
from magic_converter.core.jobs.specialized_worker import helper_main
h.com_available=lambda _:True
h.automation_enabled=lambda:False
office_engine.find_libreoffice=lambda:None
rhwp_engine.locate=lambda:None
'''


class OfficeWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.source = self.root / "document.docx"
        doc = Document(); doc.add_paragraph("Office broker keeps the source"); doc.save(self.source)
        self.original = self.source.read_bytes()

    def convert(self, key="document:pdf", source=None, notices=None):
        request = prepare_request([source or self.source], key, create_catalog())
        return execute_file(request, request.files[0], None, lambda _: None,
                            notices.append if notices is not None else lambda _: None)

    def command(self, program):
        return patch.object(worker, "worker_command", side_effect=lambda a, b: [sys.executable, "-c", program, str(a), str(b)])

    @staticmethod
    def export(source, destination, operation, *, validate):
        assert native_proxy.get() is None and operation == "office_pdf" and validate is False
        doc = PdfWriter(); doc.add_blank_page(width=72, height=72); doc.write(destination)

    def test_broker_keeps_native_export_in_parent_and_forwards_failure_to_fallback(self):
        program = BOOTSTRAP + "raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))"
        notices = []
        pass
        self.assertEqual(native.call_count, 1)
        self.assertEqual(len(PdfReader(output).pages), 1)
        self.assertTrue(any(s.startswith("PDF 구조 검사:") for s in notices))
        output.unlink()
        pass
        self.assertEqual(len(PdfReader(output).pages), 1)
        self.assertTrue(any("실패" in s for s in notices))
        self.assertEqual(self.source.read_bytes(), self.original)

    def test_preprocessing_validation_and_audit_stalls_are_interrupted_with_children(self):
        for function, key in (("read_content", "document:hwpx"), ("validate_output", "document:pdf"), ("audit_output", "document:pdf")):
            for timeout in (False, True):
                with self.subTest(function=function, timeout=timeout):
                    ready = self.root / "ready.json"
                    program = BOOTSTRAP + '''
import json,os,subprocess,time
from pathlib import Path
def blocked(*args,**kwargs):
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    marker=Path(READY+'.tmp')
    marker.write_text(json.dumps({'worker':os.getpid(),'child':child.pid}))
    marker.replace(READY)
    time.sleep(30)
setattr(h,FUNCTION,blocked)
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
'''.replace("READY", repr(str(ready))).replace("FUNCTION", repr(function))
                    control = Control(threading.Event(), lambda _: None)
                    token = current.set(control); clocks = {}
                    def interrupt():
                        until = time.monotonic() + 15
                        while not ready.is_file() and time.monotonic() < until: time.sleep(.01)
                        clocks["requested"] = time.monotonic()
                        if timeout: control.deadline = time.monotonic() + .05
                        else: control.event.set()
                    signal = threading.Thread(target=interrupt); signal.start()
                    try:
                        pass
                    finally:
                        signal.join(16); current.reset(token)
                    self.assertTrue(ready.is_file(), "Did not enter the actual library stub")
                    self.assertLess(time.monotonic() - clocks["requested"], 3)
                    pids = json.loads(ready.read_text())
                    self.assertFalse(alive(pids["worker"])); self.assertFalse(alive(pids["child"]))
                    ready.unlink()
                    self.assertEqual(list(self.root.iterdir()), [self.source])
                    self.assertEqual(self.source.read_bytes(), self.original)

    @unittest.skipUnless(os.name == "nt", "Windows native ownership ledger")
    def test_cancel_during_native_export_cleans_ledger_server_and_preserves_unrelated_process(self):
        import win32api, win32process
        for timeout in (False, True):
            with self.subTest(timeout=timeout):
                servers = [subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                           creationflags=subprocess.CREATE_NO_WINDOW) for _ in range(2)]
                server, unrelated = servers
                ready = self.root / "native-ready.json"
                outer = self.root / "outer.json"
                handle = win32api.OpenProcess(0x0400 | 0x0010, False, server.pid)
                try: identity = {"pid": server.pid, "created": str(win32process.GetProcessTimes(handle)["CreationTime"])}
                finally: handle.Close()
                native_program = '''
import json,os,subprocess,sys,time
from pathlib import Path
report=Path(sys.argv[2])
report.with_suffix('.processes.json').write_text(sys.argv[3])
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
marker=Path(sys.argv[4]+'.tmp');marker.write_text(json.dumps({'helper':os.getpid(),'child':child.pid}));marker.replace(sys.argv[4])
time.sleep(30)
'''
                def supervise(command, state):
                    request, report = command[-2:]
                    return run_helper([sys.executable, "-c", native_program, request, report,
                                       json.dumps({"owned": [identity]}), str(ready)], state)
                program = BOOTSTRAP + "\nimport os,json\nfrom pathlib import Path\nPath(" + repr(str(outer)) + ").write_text(json.dumps({'pid':os.getpid()}))\nraise SystemExit(helper_main(sys.argv[1],sys.argv[2]))"
                control = Control(threading.Event(), lambda _: None); token = current.set(control)
                def interrupt():
                    until = time.monotonic() + 15
                    while not ready.is_file() and time.monotonic() < until: time.sleep(.01)
                    if timeout: control.deadline = time.monotonic() + .05
                    else: control.event.set()
                signal = threading.Thread(target=interrupt); signal.start()
                try:
                    pass
                    self.assertTrue(ready.is_file())
                    server.wait(timeout=3)
                    self.assertIsNone(unrelated.poll())
                    self.assertFalse(alive(json.loads(outer.read_text())["pid"]))
                    for pid in json.loads(ready.read_text()).values(): self.assertFalse(alive(pid))
                finally:
                    signal.join(16); current.reset(token)
                    for process in servers:
                        if process.poll() is None: process.terminate()
                        process.wait(timeout=3)
                    ready.unlink(missing_ok=True); outer.unlink(missing_ok=True)
                self.assertEqual(list(self.root.iterdir()), [self.source])
                self.assertEqual(self.source.read_bytes(), self.original)

    def test_native_request_requires_identity_scoped_paths_and_single_delivery(self):
        folder = self.root / "worker"; folder.mkdir(); (folder / "native").mkdir(); (folder / "output").mkdir()
        original = self.source; run_id = "current-run"; handled = set()
        good = {"run_id": run_id, "source": str(original), "destination": str(folder / "output/result.pdf"), "operation": "office_pdf"}
        pass
        pass
        self.assertFalse((self.root / "outside.pdf").exists())

    def test_new_archive_and_subtitle_routes_preserve_empty_folder_and_normal_output(self):
        empty = self.root / "empty.zip"
        with zipfile.ZipFile(empty, "w"): pass
        folder = self.convert("tool:archive_extract", empty)
        self.assertTrue(folder.is_dir()); self.assertEqual(list(folder.iterdir()), [])
        subtitle = self.root / "captions.srt"
        subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
        self.assertIn("Hello", self.convert("subtitle:vtt", subtitle).read_text(encoding="utf-8-sig"))

    def test_archive_above_256_mib_retains_existing_budget_and_original_bytes(self):
        source = self.root / "large.zip"
        expected = hashlib.sha256(); chunk = bytes(1024 * 1024)
        with zipfile.ZipFile(source, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=1) as archive:
            with archive.open("payload.bin", "w") as stream:
                for _ in range(257): stream.write(chunk); expected.update(chunk)
        original = source.read_bytes()
        output = self.convert("tool:archive_extract", source) / "payload.bin"
        self.assertEqual(output.stat().st_size, 257 * 1024 * 1024)
        self.assertEqual(worker.sha(output), expected.hexdigest())
        self.assertEqual(source.read_bytes(), original)

    def test_exact_member_count_allows_root_and_refuses_one_excess_member(self):
        good, bad = self.root / "two.zip", self.root / "three.zip"
        for source, count in ((good, 2), (bad, 3)):
            with zipfile.ZipFile(source, "w") as archive:
                for index in range(count): archive.writestr(str(index), b"x")
        with patch.object(worker, "MAX_OUTPUT_ENTRIES", 2):
            output = self.convert("tool:archive_extract", good)
            self.assertEqual(len(list(output.iterdir())), 2)
            with self.assertRaises((ValueError, RuntimeError)): self.convert("tool:archive_extract", bad)
        self.assertFalse((self.root / "three_converted").exists())


if __name__ == "__main__": unittest.main()
