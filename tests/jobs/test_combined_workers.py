"""Combined jobs retain order/atomic naming and stop non-cooperative libraries."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import replace
from unittest.mock import patch

import pymupdf
from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.jobs import specialized_worker as worker
from magic_converter.core.jobs.combined_worker import execute
from magic_converter.core.jobs.job_control import Control, current, JobCancelled, JobTimedOut
from magic_converter.core.settings.preferences import OutputRule
from tests.jobs.test_parser_workers import alive


class CombinedWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.paths = []
        for name in ("One", "Two"):
            path = self.root / (name + ".pdf")
            with pymupdf.open() as doc:
                doc.new_page().insert_text((10, 30), name); doc.save(path)
            self.paths.append(path)
        self.originals = {p: p.read_bytes() for p in self.paths}

    def run_job(self, key, request=None):
        request = request or prepare_request(self.paths, key, create_catalog())
        return execute(request, lambda _: None, lambda _: None)

    def test_order_collision_and_custom_rule_survive_private_encoding(self):
        request = prepare_request(self.paths[::-1], "tool:pdf_merge", create_catalog(),
                  output=OutputRule("folder", str(self.root / "out"), "{stem}_{tag}"))
        first = self.run_job("tool:pdf_merge", request)
        second = self.run_job("tool:pdf_merge", request)
        self.assertNotEqual(first, second)
        with pymupdf.open(first) as doc:
            self.assertEqual([p.get_text().strip() for p in doc], ["Two", "One"])
        self.assertEqual({p: p.read_bytes() for p in self.paths}, self.originals)

    def test_zip_unregistered_input_and_duplicate_names_preserve_bytes(self):
        paths = []
        for index in range(2):
            folder = self.root / str(index); folder.mkdir()
            path = folder / "same.unknown"; path.write_bytes(bytes([index]) * 20); paths.append(path)
        result = self.run_job("zip", prepare_request(paths, "zip", create_catalog()))
        with zipfile.ZipFile(result) as archive:
            self.assertEqual(archive.namelist(), ["same.unknown", "same(1).unknown"])
            self.assertEqual([archive.read(n) for n in archive.namelist()], [p.read_bytes() for p in paths])

    def test_cancel_and_deadline_stop_pdf_and_zip_libraries_and_children(self):
        for key in ("tool:pdf_merge", "zip"):
            for timeout in (False, True):
                with self.subTest(key=key, timeout=timeout):
                    ready = self.root / "ready.json"
                    program = '''
import json,os,subprocess,sys,time
from pathlib import Path
from magic_converter.core.jobs.specialized_worker import helper_main
from magic_converter.core.conversion import converter
from magic_converter.core.pdf import pdf_tools
def blocked(*args,**kwargs):
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    marker=Path(READY+'.tmp');marker.write_text(json.dumps({'worker':os.getpid(),'child':child.pid}));marker.replace(READY)
    time.sleep(30)
converter.create_zip=blocked
pdf_tools.merge_pdf=blocked
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
'''.replace("READY", repr(str(ready)))
                    control = Control(threading.Event(), lambda _: None); clocks = {}
                    token = current.set(control)
                    def interrupt():
                        end = time.monotonic() + 15
                        while not ready.is_file() and time.monotonic() < end: time.sleep(.01)
                        clocks["request"] = time.monotonic()
                        if timeout: control.deadline = time.monotonic() + .05
                        else: control.event.set()
                    thread = threading.Thread(target=interrupt); thread.start()
                    try:
                        with patch.object(worker, "worker_command", side_effect=lambda a,b:[sys.executable,"-c",program,str(a),str(b)]), self.assertRaises(JobTimedOut if timeout else JobCancelled):
                            self.run_job(key)
                    finally:
                        thread.join(16); current.reset(token)
                    self.assertTrue(ready.is_file()); self.assertLess(time.monotonic() - clocks["request"], 3)
                    for pid in json.loads(ready.read_text()).values(): self.assertFalse(alive(pid))
                    ready.unlink()
                    self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, self.originals)
        self.assertTrue(self.run_job("zip").is_file())

    def test_corrupt_result_is_refused_before_publication(self):
        program = '''
import json,sys
from pathlib import Path
from magic_converter.core.jobs.specialized_worker import helper_main
code=helper_main(sys.argv[1],sys.argv[2])
request=json.loads(Path(sys.argv[1]).read_text())
Path(request['destination']).write_bytes(b'changed')
raise SystemExit(code)
'''
        for key in ("zip", "tool:pdf_merge"):
            with self.subTest(key=key), patch.object(worker, "worker_command", side_effect=lambda a,b:[sys.executable,"-c",program,str(a),str(b)]), self.assertRaises(RuntimeError):
                self.run_job(key)
            self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, self.originals)


if __name__ == "__main__": unittest.main()
