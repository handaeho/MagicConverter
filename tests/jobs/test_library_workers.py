"""Local library stalls and file/tree results through the real worker boundary."""

import json
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

import pymupdf
from docx import Document
from openpyxl import Workbook
from PIL import Image

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_execution import execute_file
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs import specialized_worker as worker
from magic_converter.core.jobs.job_control import Control, current, JobCancelled, JobTimedOut
from magic_converter.core.settings.preferences import EncoderOptions, OutputRule, ToolOptions
from tests.jobs.test_parser_workers import alive


class LibraryWorkerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.png = self.root / "image.png"
        Image.new("RGB", (32, 16), "red").save(self.png)
        self.pdf = self.root / "pages.pdf"
        with pymupdf.open() as doc:
            for text in ("One", "Two"):
                doc.new_page().insert_text((20, 40), text)
            doc.save(self.pdf)
        self.docx = self.root / "document.docx"
        doc = Document(); doc.add_paragraph("Worker preserves this text"); doc.save(self.docx)
        self.inputs = {p: p.read_bytes() for p in self.root.iterdir()}

    def convert(self, source, key, *, request=None, progress=None, notice=None):
        request = request or prepare_request([source], key, create_catalog())
        return execute_file(request, request.files[0], None, progress or (lambda _: None), notice or (lambda _: None))

    def test_normal_image_document_and_editable_pdf_with_progress(self):
        values = []
        output = self.convert(self.png, "image:jpg", progress=values.append)
        with Image.open(output) as image:
            self.assertEqual(image.size, (32, 16))
        self.assertEqual(Document(self.convert(self.docx, "document:docx")).paragraphs[0].text,
                         "Worker preserves this text")
        from pptx import Presentation
        deck = Presentation(self.convert(self.pdf, "pptx_editable", progress=values.append))
        self.assertEqual(len(deck.slides), 2)
        self.assertTrue(any("One" in s.text for s in deck.slides[0].shapes if s.has_text_frame))
        self.assertIn(100, values)

    def test_folder_results_keep_pdf_pages_frames_and_multiple_sheets(self):
        pages = self.convert(self.pdf, "document:png")
        self.assertEqual(len(list(pages.glob("*.png"))), 2)
        animation = self.root / "animation.png"
        with Image.new("RGB", (4, 4), "red") as first, Image.new("RGB", (4, 4), "blue") as second:
            first.save(animation, save_all=True, append_images=[second], duration=[40, 80], loop=2)
        frames = self.convert(animation, "tool:image_frames")
        manifest = json.loads((frames / "frames.json").read_text(encoding="utf-8"))
        self.assertEqual([f["duration_ms"] for f in manifest["frames"]], [40, 80])
        self.assertEqual(len(list(frames.glob("*.png"))), 2)
        sheets = self.root / "sheets.xlsx"
        workbook = Workbook(); workbook.active.append(["first", 42]); workbook.create_sheet("Second").append(["second", 24])
        workbook.save(sheets); workbook.close()
        exported = self.convert(sheets, "sheet:csv")
        self.assertEqual(len(list(exported.glob("*.csv"))), 2)
        self.assertTrue(any("second" in p.read_text(encoding="utf-8-sig") for p in exported.glob("*.csv")))

    def test_headless_qt_text_pdf_and_svg_fonts_survive_worker_startup(self):
        text = self.root / "notes.txt"
        text.write_text("Text through a headless Qt font database", encoding="utf-8")
        pages = self.convert(text, "document:png")
        self.assertEqual(len(list(pages.glob("*.png"))), 1)
        svg = self.root / "label.svg"
        svg_text = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="30"><text x="1" y="20">{}</text></svg>'
        rendered = []
        for label in ("WWWW", "IIII"):
            svg.write_text(svg_text.format(label), encoding="utf-8")
            with Image.open(self.convert(svg, "image:png")) as image:
                self.assertEqual(image.size, (100, 30))
                self.assertIsNotNone(image.getbbox())
                rendered.append(image.tobytes())
        self.assertNotEqual(*rendered)

    def test_serialized_encoder_tool_output_options_and_collision(self):
        request = prepare_request([self.png], "image:jpg", create_catalog())
        request = replace(request, options=EncoderOptions(image_quality=73, image_max_edge=8),
                          output=OutputRule("folder", str(self.root / "outputs"), "{stem}_{tag}"))
        first = self.convert(self.png, "image:jpg", request=request)
        original = first.read_bytes()
        second = self.convert(self.png, "image:jpg", request=request)
        self.assertNotEqual(first, second)
        self.assertEqual(first.read_bytes(), original)
        with Image.open(second) as image:
            self.assertEqual(image.size, (8, 4))
        request = prepare_request([self.pdf], "tool:pdf_rotate", create_catalog())
        request = replace(request, tools=ToolOptions(rotation=180, pdf_pages="2"))
        with pymupdf.open(self.convert(self.pdf, "tool:pdf_rotate", request=request)) as doc:
            self.assertEqual([p.rotation for p in doc], [0, 180])

    def test_cancel_and_deadline_interrupt_image_pdf_document_calls_and_owned_children(self):
        program = '''
import json,os,subprocess,sys,time
from pathlib import Path
from magic_converter.core.conversion import converter,extended_converter
from magic_converter.core.jobs.specialized_worker import helper_main
def blocked(source,destination,*args,**kwargs):
    destination.write_bytes(b'partial')
    child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
    marker=Path(sys.argv[3]+'.tmp')
    marker.write_text(json.dumps({'worker':os.getpid(),'child':child.pid}))
    marker.replace(sys.argv[3])
    time.sleep(30)
module=converter if sys.argv[4]=='convert_image' else extended_converter
setattr(module,sys.argv[4],blocked)
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
'''
        for source, key, function in ((self.png, "image:jpg", "convert_image"),
                                      (self.pdf, "document:docx", "pdf_to_docx"),
                                      (self.docx, "document:html", "convert_text_document")):
            for timeout in (False, True):
                with self.subTest(function=function, timeout=timeout):
                    ready = self.root / "ready.json"
                    control = Control(threading.Event(), lambda _: None)
                    token = current.set(control)
                    clocks = {}
                    def interrupt():
                        end = time.monotonic() + 15
                        while not ready.is_file() and time.monotonic() < end:
                            time.sleep(.01)
                        clocks["requested"] = time.monotonic()
                        if timeout: control.deadline = time.monotonic() + .05
                        else: control.event.set()
                    signal = threading.Thread(target=interrupt); signal.start()
                    try:
                        command = lambda a, b: [sys.executable, "-c", program, str(a), str(b), str(ready), function]
                        with patch.object(worker, "worker_command", side_effect=command), self.assertRaises(JobTimedOut if timeout else JobCancelled):
                            self.convert(source, key)
                    finally:
                        signal.join(16); current.reset(token)
                    self.assertTrue(ready.is_file(), "Did not enter the blocked library")
                    self.assertLess(time.monotonic() - clocks["requested"], 3)
                    pids = json.loads(ready.read_text())
                    self.assertFalse(alive(pids["worker"]))
                    self.assertFalse(alive(pids["child"]))
                    ready.unlink()
                    self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, self.inputs)
        self.assertTrue(self.convert(self.png, "image:jpg").is_file())

    def test_changed_folder_result_is_refused_and_next_conversion_works(self):
        program = '''
import json,sys
from pathlib import Path
from magic_converter.core.jobs.specialized_worker import helper_main
code=helper_main(sys.argv[1],sys.argv[2])
r=json.loads(Path(sys.argv[1]).read_text())
p=next(Path(r['destination']).glob('*.png'))
p.write_bytes(b'changed')
raise SystemExit(code)
'''
        command = lambda a, b: [sys.executable, "-c", program, str(a), str(b)]
        with patch.object(worker, "worker_command", side_effect=command), self.assertRaises(ConversionError):
            self.convert(self.pdf, "document:png")
        self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, self.inputs)
        self.assertTrue(self.convert(self.pdf, "document:png").is_dir())

    def test_aggregate_folder_budget_refuses_partial_tree_without_publication(self):
        program = '''
import json,sys,time
from pathlib import Path
r=json.loads(Path(sys.argv[1]).read_text())
p=Path(r['destination']);p.mkdir()
(p/'one').write_bytes(b'x'*700)
(p/'two').write_bytes(b'x'*700)
time.sleep(30)
'''
        command = lambda a, b: [sys.executable, "-c", program, str(a), str(b)]
        with patch.object(worker, "WORKER_OUTPUT_BYTES", 1024), patch.object(worker, "worker_command", side_effect=command), self.assertRaises(ConversionError):
            self.convert(self.pdf, "document:png")
        self.assertEqual({p: p.read_bytes() for p in self.root.iterdir()}, self.inputs)


if __name__ == "__main__":
    unittest.main()
