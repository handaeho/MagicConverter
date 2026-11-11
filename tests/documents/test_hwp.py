"""HWP routing, genuine OLE records, OWPML tables, and owned COM cleanup."""

import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from docx import Document
from docx.shared import Pt, RGBColor
from openpyxl import Workbook
from pptx import Presentation
from PySide6.QtWidgets import QApplication

from magic_converter.core.conversion.converter import ConversionError, ConversionWorker
from magic_converter.core.conversion.extended_converter import convert_extended
from magic_converter.core.conversion.file_router import ALL_ACTIONS, slots_for
from magic_converter.core.engines.hwp_converter import (convert_hwp, convert_office_pdf,
                                                   office_content, validate_output)
from magic_converter.core.documents.hwp_documents import (DocumentData, Paragraph, TextRun, Table, Cell, read_docx,
    read_hwpx, read_hwp, write_docx, write_hwpx, decode_hwp_text, hwp_records, q)
from magic_converter.core.engines.office_com import application, com_apartment, word_save

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def make_hwp(path, text="한글 원본 본문", compressed=True, encrypted=False):
    """Small real CFB v3 container; no olefile writer/mocked streams required."""
    free, end = 0xFFFFFFFF, 0xFFFFFFFE
    header = bytearray(512)
    header[:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 1, 0, 4096, end, 0, end, 0)
    struct.pack_into("<109I", header, 76, 0, *([free] * 108))
    fat = [free] * 128
    fat[0], fat[1] = 0xFFFFFFFD, end
    for start in (2, 10):
        for i in range(start, start + 7):
            fat[i] = i + 1
        fat[start + 7] = end
    def entry(name, kind, child=free, right=free, start=end, size=0):
        value = bytearray(128)
        encoded = (name + "\0").encode("utf-16le")
        value[:len(encoded)] = encoded
        struct.pack_into("<HBBIII", value, 64, len(encoded), kind, 1, free, right, child)
        struct.pack_into("<IQ", value, 116, start, size)
        return value
    directory = (entry("Root Entry", 5, child=1) + entry("FileHeader", 2, right=2, start=2, size=4096)
                 + entry("BodyText", 1, child=3) + entry("Section0", 2, start=10, size=4096))
    info = bytearray(4096)
    info[:17] = b"HWP Document File"
    struct.pack_into("<I", info, 32, 0x05000000)
    struct.pack_into("<I", info, 36, int(compressed) | (2 if encrypted else 0))
    payload = (text + "\r").encode("utf-16le")
    records = struct.pack("<I", 67 | len(payload) << 20) + payload
    if compressed:
        encoder = zlib.compressobj(wbits=-15)
        records = encoder.compress(records) + encoder.flush()
    else:
        padding = 4096 - len(records) - 4
        records += struct.pack("<I", padding << 20) + b"\0" * padding
    path.write_bytes(header + struct.pack("<128I", *fat) + directory + info + records.ljust(4096, b"\0"))


class HwpTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        rhwp = patch("magic_converter.core.engines.rhwp_engine.locate", return_value=None)
        rhwp.start()
        self.addCleanup(rhwp.stop)

    def tearDown(self):
        self.temp.cleanup()

    def docx(self):
        path = self.root / "한글 문서.docx"
        doc = Document()
        run = doc.add_paragraph().add_run("문단 가나다 ABC")
        run.bold, run.italic = True, True
        run.font.size, run.font.color.rgb = Pt(14), RGBColor(12, 34, 56)
        table = doc.add_table(rows=3, cols=3)
        table.cell(0, 0).merge(table.cell(0, 1)).text = "가로 병합"
        table.cell(1, 0).merge(table.cell(2, 0)).text = "세로 병합"
        table.cell(1, 1).text = "표 내용"
        table.cell(2, 2).text = "마지막 셀"
        doc.add_paragraph("마지막 문단")
        doc.save(path)
        return path

    def test_routes_all_requested_slots_and_requires_rhwp_only_for_binary_hwp(self):
        for ext, expected in {"hwp": {"DOCX", "PDF", "HWPX", "TXT"}, "hwpx": {"DOCX", "PDF", "HWP", "TXT"}}.items():
            slots = slots_for(["file." + ext], "convert", rhwp_available=False)
            self.assertEqual({s.action.label for s in slots}, expected)
            self.assertTrue(all(s.enabled for s in slots if s.action.label != "HWP"))
        for ext in ("docx", "pptx", "xlsx"):
            slots = {s.action.label: s for s in slots_for(["file." + ext], "convert", rhwp_available=False)}
            self.assertIn("PDF", slots)
            self.assertIn("HWP", slots)
            self.assertFalse(slots["HWP"].enabled)
            if ext == "docx":
                self.assertTrue(slots["HWPX"].enabled)
        mixed = slots_for(["a.hwp", "b.hwpx"], "convert")
        self.assertEqual({s.action.label for s in mixed}, {"DOCX", "PDF", "TXT"})

    def test_docx_hwpx_roundtrip_preserves_order_runs_and_merged_cells(self):
        source = self.docx()
        original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
        before = read_docx(source)
        path = self.root / "한글.hwpx"
        write_hwpx(before, path)
        validate_output(path)
        after = read_hwpx(path)
        self.assertEqual(before.text(), after.text())
        self.assertEqual([type(b) for b in after.blocks], [Paragraph, Table, Paragraph])
        self.assertEqual(after.blocks[0].runs[0], before.blocks[0].runs[0])
        self.assertEqual([(c.row, c.col, c.rowspan, c.colspan) for c in before.blocks[1].cells],
                         [(c.row, c.col, c.rowspan, c.colspan) for c in after.blocks[1].cells])
        with zipfile.ZipFile(path) as archive:
            self.assertEqual(archive.infolist()[0].filename, "mimetype")
            self.assertEqual(archive.infolist()[0].compress_type, zipfile.ZIP_STORED)
            self.assertEqual(archive.read("mimetype"), b"application/hwp+zip")
            for name in ("Contents/header.xml", "Contents/section0.xml"):
                self.assertIn(b'standalone="yes"', archive.read(name))
                self.assertIn(b'xmlns:hp10=', archive.read(name))
            for name in archive.namelist():
                if name.endswith((".xml", ".hpf", ".rdf")):
                    ET.fromstring(archive.read(name))
            section = ET.fromstring(archive.read("Contents/section0.xml"))
            self.assertIsNotNone(section.find(".//" + q("hp", "secPr")))
            ids = [p.get("id") for p in section.iter(q("hp", "p"))]
            self.assertEqual(len(ids), len(set(ids)))
            self.assertTrue(all(int(i) < 2**31 for i in ids))
        target = self.root / "roundtrip.docx"
        write_docx(after, target)
        restored = Document(target)
        self.assertEqual(restored.tables[0].cell(0, 0).text, "가로 병합")
        self.assertEqual(restored.tables[0].cell(2, 0).text, "세로 병합")
        self.assertEqual(original_hash, hashlib.sha256(source.read_bytes()).hexdigest())

    def test_hwpx_manifest_section_order_and_no_duplicate_cell_paragraphs(self):
        path = self.root / "sections.hwpx"
        write_hwpx(DocumentData([Paragraph([TextRun("첫째")])]), path)
        with zipfile.ZipFile(path) as archive:
            files = {n: archive.read(n) for n in archive.namelist()}
        for i, text in ((2, "둘째"), (10, "열째")):
            files[f"Contents/section{i}.xml"] = files["Contents/section0.xml"].replace("첫째".encode(), text.encode())
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in files.items():
                archive.writestr(name, data)
        self.assertEqual(read_hwpx(path).text(), "첫째\n둘째\n열째")

    def test_real_ole_hwp_compressed_and_uncompressed_text_and_encryption(self):
        for compressed in (False, True):
            path = self.root / f"sample-{compressed}.hwp"
            make_hwp(path, compressed=compressed)
            self.assertEqual(read_hwp(path).text(), "한글 원본 본문")
        make_hwp(path, encrypted=True)
        with self.assertRaisesRegex(ConversionError, "암호/배포"):
            read_hwp(path)

    def test_hwp_inline_controls_and_truncated_records(self):
        payload = "앞".encode("utf-16le") + struct.pack("<8H", 2, *([0xAC00] * 7)) + "뒤\r".encode("utf-16le")
        self.assertEqual(decode_hwp_text(payload), "앞뒤")
        with self.assertRaises(ConversionError):
            list(hwp_records(struct.pack("<I", 67 | 100 << 20) + b"x"))

    def test_hwp_and_hwpx_fallbacks_have_real_formats_and_warnings(self):
        source = self.root / "원본.hwp"
        make_hwp(source)
        with patch("magic_converter.core.engines.rhwp_engine.locate", return_value=None):
            notices = []
            hwpx = convert_extended(source, ALL_ACTIONS["document:hwpx"], lambda _: None, notices.append)
            self.assertTrue(any("문서 모델" in notice for notice in notices))
            for key in ("document:docx", "document:txt", "document:pdf"):
                output = convert_extended(hwpx, ALL_ACTIONS[key], lambda _: None, notices.append)
                self.assertTrue(output.stat().st_size)
            self.assertEqual(Document(hwpx.with_suffix(".docx").with_stem(hwpx.stem + "_converted")).paragraphs[0].text, "한글 원본 본문")

    def test_no_fake_binary_hwp_and_worker_propagates_fallback_notice(self):
        source = self.docx()
        with patch("magic_converter.core.engines.rhwp_engine.locate", return_value=None):
            with self.assertRaisesRegex(ConversionError, "rhwp"):
                convert_extended(source, ALL_ACTIONS["document:hwp"], lambda _: None)
            results = []
            worker = ConversionWorker(501, (source,), ALL_ACTIONS["document:hwpx"], None)
            worker.signals.finished.connect(results.append)
            program = '''
import sys
from magic_converter.core.engines import hwp_converter as h,rhwp_engine
from magic_converter.core.jobs.specialized_worker import helper_main
rhwp_engine.locate=lambda:None
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
'''
            with patch("magic_converter.core.jobs.specialized_worker.worker_command", side_effect=lambda a,b: [sys.executable,"-c",program,str(a),str(b)]):
                worker.run()
            self.assertFalse(results[0].errors)
            self.assertTrue(any("문서 모델" in notice for notice in results[0].warnings))
            self.assertEqual(read_hwpx(results[0].outputs[0]).blocks[0].text, "문단 가나다 ABC")
            self.assertFalse(any(self.root.glob("*.hwp")))


    def test_com_initialization_balanced_for_worker_and_exception(self):
        import pythoncom
        with patch.object(pythoncom, "CoInitialize") as initialize, patch.object(pythoncom, "CoUninitialize") as finalize:
            with self.assertRaises(RuntimeError), com_apartment():
                raise RuntimeError("error")
            initialize.assert_called_once()
            finalize.assert_called_once()
        with patch.object(pythoncom, "CoInitialize") as initialize, patch.object(pythoncom, "CoUninitialize") as finalize:
            worker = ConversionWorker(1, (self.root / "missing.docx",), ALL_ACTIONS["document:hwpx"], None)
            worker.run()
            initialize.assert_called_once()
            finalize.assert_called_once()


    def test_word_closes_document_and_quits_when_save_fails(self):
        app, ledger = MagicMock(), MagicMock()
        app.Documents.Count = 0
        document = app.Documents.Open.return_value
        document.SaveAs2.side_effect = RuntimeError("SaveAs failed")
        from contextlib import nullcontext
        with patch('magic_converter.core.engines.office_activation.bounded_application',return_value=nullcontext(app)), self.assertRaises(RuntimeError):
            word_save(self.docx(), self.root / "out.rtf", 6, ledger)
        document.Close.assert_called_once_with(False)
        app.Quit.assert_called_once()

    def test_reused_powerpoint_refuses_input_and_keeps_users_settings(self):
        app, ledger = MagicMock(), MagicMock()
        app.Presentations.Count = 1
        app.DisplayAlerts, app.AutomationSecurity = 2, 1
        from contextlib import nullcontext
        with patch('magic_converter.core.engines.office_activation.bounded_application',return_value=nullcontext(app)):
            with self.assertRaisesRegex(RuntimeError, "기존"), application("powerpoint", ledger):
                self.fail("Existing server yielded input")
        app.Quit.assert_not_called()
        self.assertEqual(app.DisplayAlerts, 2)
        self.assertEqual(app.AutomationSecurity, 1)
        ledger.exclude.assert_called_once_with(app)
        ledger.track.assert_not_called()

    def test_com_timeout_always_cleans_only_recorded_processes(self):
        with patch("magic_converter.core.engines.hwp_converter.run_helper", side_effect=TimeoutError("Office 응답 제한 180초")), patch(
                "magic_converter.core.engines.hwp_converter.cleanup_owned_processes") as cleanup:
            from magic_converter.core.engines.hwp_converter import run_com
            with self.assertRaisesRegex(ConversionError, "180초"):
                run_com(self.docx(), self.root / "output.pdf", "office_pdf")
        cleanup.assert_called_once()
        self.assertEqual(cleanup.call_args.args[0].name, "report.processes.json")

    def test_powerpoint_method_hwnd_tracks_one_owned_identity(self):
        from magic_converter.core.engines.office_com import ProcessLedger
        ledger = ProcessLedger(self.root / "ledger.json")
        ledger.before = set()
        app = MagicMock()
        app.HWND.return_value = 123
        with patch("win32process.GetWindowThreadProcessId", return_value=(1, 12345)), patch(
                "win32process.GetProcessTimes", return_value={"CreationTime": "unique-created-time"}), patch(
                "win32api.OpenProcess"), patch.object(ledger.budget, 'attach', return_value=True) as attach:
            ledger.track(app, "powerpoint")
            ledger.track(app, "powerpoint")
            self.assertEqual(ledger.owned, [{"pid": 12345, "created": "unique-created-time",
                                           "commit_limit_bytes": 2 * 1024**3,
                                           "application": "powerpoint", "activation": "scm",
                                           "budget_phase": "after_native_identity", "preactivation_bounded": False}])
        self.assertEqual(attach.call_count, 2)
        attach.assert_called_with(12345, 'unique-created-time')

    def test_hidden_powerpoint_identity_never_changes_unverified_caption(self):
        from magic_converter.core.engines.office_com import _powerpoint_hwnd
        app = MagicMock()
        app.HWND.side_effect = RuntimeError("Hidden HWND is unavailable")
        app.Caption = "Original caption"
        with patch("win32gui.EnumWindows") as windows:
            self.assertEqual(_powerpoint_hwnd(app), 0)
        windows.assert_not_called()
        self.assertEqual(app.Caption, "Original caption")

    def test_word_tracks_document_window_not_application(self):
        from magic_converter.core.engines.office_com import ProcessLedger
        ledger = ProcessLedger(self.root / "word-ledger.json")
        ledger.before = set()
        app = MagicMock()
        app.Windows.Count = 1
        app.Windows.Item.return_value.Hwnd = 123
        with patch("win32process.GetWindowThreadProcessId", return_value=(1, 54321)), patch(
                "win32process.GetProcessTimes", return_value={"CreationTime": "word-created-time"}), patch(
                "win32api.OpenProcess"), patch.object(ledger.budget, 'attach', return_value=True) as attach:
            ledger.track(app, "word")
            self.assertEqual(ledger.owned, [{"pid": 54321, "created": "word-created-time",
                                           "commit_limit_bytes": 2 * 1024**3,
                                           "application": "word", "activation": "scm",
                                           "budget_phase": "after_native_identity", "preactivation_bounded": False}])
        attach.assert_called_once_with(54321, 'word-created-time')


    def test_slides_and_sheets_extract_tables_for_hwp_export(self):
        sheet = self.root / "sheet.xlsx"
        workbook = Workbook()
        workbook.active.append(["항목", "값"])
        workbook.active.append(["수량", 123])
        workbook.save(sheet)
        self.assertIn("수량\t123", office_content(sheet).text())
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[5])
        slide.shapes.title.text = "발표 제목"
        table = slide.shapes.add_table(2, 2, 0, 1000000, 2000000, 1000000).table
        table.cell(0, 0).text = "표 제목"
        path = self.root / "slides.pptx"
        deck.save(path)
        data = office_content(path)
        self.assertIn("발표 제목", data.text())
        self.assertIn("표 제목", data.text())

    @unittest.skipUnless(os.environ.get("MAGIC_CONVERTER_TEST_OFFICE") == "1", "Optional actual Office COM export")
    def test_real_word_excel_powerpoint_pywin32_pdf(self):
        from pypdf import PdfReader
        docx = self.docx()
        sheet = self.root / "table.xlsx"
        workbook = Workbook()
        workbook.active.append(["PDF", 42])
        workbook.save(sheet)
        slides = self.root / "deck.pptx"
        deck = Presentation()
        deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = "COM PDF"
        deck.save(slides)
        def native_only(*args, **kwargs):
            # The PDF audit legitimately reads office_content after COM export.
            # Assert the selected export engine instead of forbidding that read.
            engine = convert_office_pdf(*args, **kwargs)
            self.assertEqual(engine, "office-com")
            return engine

        with patch("magic_converter.core.engines.hwp_converter.convert_office_pdf", side_effect=native_only):
            for source, text in ((docx, "ABC"), (sheet, "PDF"), (slides, "COM PDF")):
                notices = []
                output = convert_extended(source, ALL_ACTIONS["document:pdf"], lambda _: None, notices.append)
                self.assertTrue(any(message.startswith("PDF 구조 검사:") for message in notices))
                self.assertFalse(any("실패" in message or "완료하지 못" in message for message in notices), notices)
                self.assertGreater(len(PdfReader(output).pages), 0)
                self.assertIn(text, " ".join(page.extract_text() for page in PdfReader(output).pages))


if __name__ == "__main__":
    unittest.main()
