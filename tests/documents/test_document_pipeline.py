"""Real rhwp, Office variants and independent document-loss detection."""
import io
import json
import os
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from docx import Document
from openpyxl import Workbook, load_workbook
from PIL import Image
from pptx import Presentation
from pptx.util import Inches
from PySide6.QtWidgets import QApplication

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.conversion.converter import ConversionWorker
from magic_converter.core.documents.document_audit import compare, inventory
from magic_converter.core.diagnostics.document_diagnostics import check_documents
from magic_converter.core.documents.document_math import from_omml, to_omml
from magic_converter.core.conversion.extended_converter import convert_extended
from magic_converter.core.conversion.file_router import ALL_ACTIONS, slots_for
from magic_converter.core.engines.hwp_converter import convert_hwp, read_content, validate_output
from magic_converter.core.documents.hwp_documents import (Cell, DocumentData, Equation, Paragraph, Picture, Table, TextRun,
                                                 read_docx, read_hwpx, write_docx, write_hwpx)
from magic_converter.core.jobs.job_control import Control, JobCancelled, JobTimedOut, current
from magic_converter.core.engines.office_formats import VARIANTS, TYPES, canonical, normalize
from magic_converter.core.settings.preferences import Preferences, Preset, WheelProfile
from magic_converter.core.engines import rhwp_engine
from magic_converter.core.settings.wheel_profiles import profile_slots

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class DocumentStage5Tests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self):
        raw = io.BytesIO()
        Image.new("RGB", (24, 16), "red").save(raw, "PNG")
        eq = Equation("{1} over {{x}^{2}}")
        eq.omml, _ = to_omml(eq)
        return DocumentData([Paragraph([TextRun("앞 "), eq, TextRun(" 뒤")]),
                             Table(1, 2, [Cell(0, 0, [Paragraph([Picture(raw.getvalue(), 18, 12)])], colspan=2)])])

    def variant(self, source, suffix):
        target = self.root / (source.stem + suffix)
        base = canonical(suffix)
        main = {".docx": "/word/document.xml", ".pptx": "/ppt/presentation.xml", ".xlsx": "/xl/workbook.xml"}[base]
        content_type = {
            ".docm": "application/vnd.ms-word.document.macroEnabled.main+xml",
            ".dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
            ".dotm": "application/vnd.ms-word.template.macroEnabledTemplate.main+xml",
            ".pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
            ".potx": "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
            ".potm": "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
            ".ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
            ".ppsm": "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml",
            ".xlsm": "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
            ".xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
            ".xltm": "application/vnd.ms-excel.template.macroEnabled.main+xml",
        }[suffix]
        with zipfile.ZipFile(source) as original, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as output:
            for info in original.infolist():
                data = original.read(info)
                if info.filename == "[Content_Types].xml":
                    root = ET.fromstring(data)
                    next(c for c in root if c.get("PartName") == main).set("ContentType", content_type)
                    ET.SubElement(root, "{" + root.tag.split("}")[0][1:] + "}Override", PartName="/word/vbaProject.bin", ContentType="application/vnd.ms-office.vbaProject")
                    data = ET.tostring(root)
                output.writestr(info.filename, data)
            output.writestr("word/vbaProject.bin", b"not executable test macro marker")
        return target

    def test_omml_order_fraction_root_and_scripts_are_editable(self):
        for script in ("{1} over {{x}^{2}}", "sqrt {a}", "root {3} of {b}", "{x}_{i}^{2}"):
            equation = Equation(script)
            xml, supported = to_omml(equation)
            self.assertTrue(supported)
            converted = from_omml(ET.fromstring(xml))
            self.assertEqual("".join(converted.script.split()), "".join(script.split()))
        docx = self.root / "formula.docx"
        before = self.fixture()
        write_docx(before, docx)
        after = read_docx(docx)
        self.assertEqual(after.blocks[0].text, before.blocks[0].text)
        self.assertEqual(compare(before, after), [])
        hwpx = self.root / "formula.hwpx"
        write_hwpx(after, hwpx)
        self.assertEqual(compare(after, read_hwpx(hwpx)), [])
        with zipfile.ZipFile(hwpx) as z:
            self.assertIn(b"Equation Version 60", z.read("Contents/section0.xml"))

    def test_unknown_math_is_preserved_in_docx_and_reported_for_other_formats(self):
        ns = "http://schemas.openxmlformats.org/officeDocument/2006/math"
        xml = f'<m:oMath xmlns:m="{ns}"><m:func><m:e><m:r><m:t>sin(x)</m:t></m:r></m:e></m:func></m:oMath>'
        eq = from_omml(ET.fromstring(xml))
        self.assertFalse(eq.supported)
        output, supported = to_omml(eq)
        self.assertIn(b"func", output)
        self.assertFalse(supported)
        _, supported = to_omml(Equation("matrix {a&b#c&d}"))
        self.assertFalse(supported)

    def test_loss_detection_finds_equal_count_changed_objects_and_text(self):
        before = self.fixture()
        after = self.fixture()
        after.blocks[1].cells[0].colspan = 1
        after.blocks[1].cells[0].paragraphs[0].runs[0].data = b"changed"
        after.blocks[0].runs[1].script = "{2} over {x}"
        warnings = compare(before, after)
        self.assertEqual(len(warnings), 4)
        self.assertTrue(all("손실 검사" in value for value in warnings))

    @unittest.skipUnless(rhwp_engine.locate(), "Bundled rhwp unavailable; run tools/dependencies/fetch_rhwp.py")
    def test_real_hwp_roundtrip_and_pdf_render(self):
        with patch("magic_converter.core.engines.hwp_converter.run_com", side_effect=AssertionError("native called")):
            report = check_documents(self.root)
        self.assertEqual(report["portable"][".hwp"]["losses"], [])
        self.assertEqual(report["portable"][".pdf"]["render"], "passed")

    def test_all_eleven_office_variants_have_real_package_output(self):
        word = self.root / "word.docx"
        write_docx(self.fixture(), word)
        image = self.root / "image.png"
        Image.new("RGB", (24, 16), "blue").save(image)
        slides = self.root / "slides.pptx"
        presentation = Presentation()
        slide = presentation.slides.add_slide(presentation.slide_layouts[6])
        slide.shapes.add_picture(str(image), Inches(1), Inches(1))
        slide.shapes.add_table(1, 2, Inches(1), Inches(2), Inches(3), Inches(1))
        presentation.save(slides)
        sheets = self.root / "sheet.xlsx"
        workbook = Workbook()
        workbook.active["A1"], workbook.active["B1"] = 2, "=A1*3"
        workbook.active.merge_cells("A2:B2")
        from openpyxl.drawing.image import Image as SheetImage
        workbook.active.add_image(SheetImage(image), "A3")
        workbook.save(sheets)
        for suffix in sorted(VARIANTS):
            with self.subTest(suffix=suffix):
                base = canonical(suffix)
                source = self.variant({".docx": word, ".pptx": slides, ".xlsx": sheets}[base], suffix)
                key = {".docx": "document:docx", ".pptx": "office:pptx", ".xlsx": "sheet:xlsx"}[base]
                notices = []
                output = convert_extended(source, ALL_ACTIONS[key], lambda _: None, notices.append)
                self.assertEqual(output.suffix, base)
                with zipfile.ZipFile(output) as z:
                    self.assertFalse(any("vba" in n.lower() for n in z.namelist()))
                    self.assertNotIn(b"macroEnabled", z.read("[Content_Types].xml"))
                if base == ".docx":
                    self.assertEqual(compare(read_docx(word), read_docx(output)), [])
                elif base == ".pptx":
                    shapes = Presentation(output).slides[0].shapes
                    self.assertEqual(len(shapes), 2)
                    self.assertTrue(shapes[1].has_table)
                else:
                    wb = load_workbook(output)
                    self.assertEqual(wb.active["B1"].value, "=A1*3")
                    self.assertEqual(len(wb.active._images), 1)
                    self.assertIn("A2:B2", wb.active.merged_cells)
                    wb.close()
                self.assertTrue(notices)

    def test_engine_availability_matches_custom_and_automatic_wheels(self):
        no_engines = create_catalog(rhwp_available=False)
        available = create_catalog(rhwp_available=True)
        self.assertIsNone(no_engines.assess("source.hwpx", "document:hwp").route)
        self.assertIsNotNone(available.assess("source.hwpx", "document:hwp").route)
        preferences = replace(Preferences(), presets=(Preset("hwp", "HWP", "document:hwp"),),
                              profiles=(WheelProfile("hwp", "HWP", "convert", ("hwp",)),), convert_profile="hwp")
        for rhwp in (False, True):
            slots = profile_slots(["source.dotm"], "convert", preferences, rhwp=rhwp)
            self.assertEqual(slots[0].enabled, rhwp)
            standard = slots_for(["source.dotm"], "convert", rhwp_available=rhwp)
            self.assertEqual(next(s.enabled for s in standard if s.action.key == "document:hwp"), rhwp)

    def test_rhwp_timeout_cancel_and_failure_do_not_publish_a_file(self):
        source = self.root / "source.hwpx"
        write_hwpx(self.fixture(), source)
        for error in (JobCancelled(), JobTimedOut("timed out")):
            with self.subTest(error=type(error).__name__), patch.object(rhwp_engine, "run_process", side_effect=error):
                with self.assertRaises(type(error)):
                    convert_hwp(source, self.root / "cancelled.hwp", lambda _: None)
                self.assertFalse((self.root / "cancelled.hwp").exists())
        self.assertGreater(rhwp_engine.time_limit(source), 120)
        self.assertTrue(rhwp_engine.shaping_required("العربية"))
        self.assertFalse(rhwp_engine.shaping_required("한글 Japanese 日本語"))

    def test_bad_office_packages_fail_before_publication(self):
        source = self.root / "fake.docm"
        source.write_bytes(b"renamed text is not a document")
        with self.assertRaises(zipfile.BadZipFile):
            convert_extended(source, ALL_ACTIONS["document:docx"], lambda _: None)
        self.assertEqual(list(self.root.iterdir()), [source])

    def test_removed_embedded_objects_leave_no_dangling_document_relationships(self):
        source = self.root / "embedded.docx"
        Document().save(source)
        with zipfile.ZipFile(source) as z:
            files = {n: z.read(n) for n in z.namelist()}
        rns = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
        wns = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
        rels = ET.fromstring(files["word/_rels/document.xml.rels"])
        ET.SubElement(rels, "{" + rels.tag.split("}")[0][1:] + "}Relationship", Id="rIdRemoved", Type=rns + "/oleObject", Target="embeddings/embedded.bin")
        files["word/_rels/document.xml.rels"] = ET.tostring(rels)
        doc = ET.fromstring(files["word/document.xml"])
        obj = ET.SubElement(doc.find(f"{{{wns}}}body"), f"{{{wns}}}object")
        ET.SubElement(obj, "{urn:test}OLEObject", {f"{{{rns}}}id": "rIdRemoved"})
        files["word/document.xml"] = ET.tostring(doc)
        files["word/embeddings/embedded.bin"] = b"object marker"
        source = self.root / "embedded.docm"
        with zipfile.ZipFile(source, "w") as z:
            for n, b in files.items():
                z.writestr(n, b)
        target = self.root / "normalized.docx"
        normalize(source, target)
        with zipfile.ZipFile(target) as z:
            self.assertNotIn("word/embeddings/embedded.bin", z.namelist())
            self.assertNotIn(b"rIdRemoved", z.read("word/document.xml"))
            self.assertNotIn(b"rIdRemoved", z.read("word/_rels/document.xml.rels"))
        self.assertEqual(len(Document(target).paragraphs), 0)



    def test_office_state_replace_retries_only_temporary_permission_errors(self):
        from magic_converter.core.engines.office_com import _save_json
        path = self.root / "state.json"
        import os
        original = os.replace
        calls = []
        def locked_once(source, target):
            calls.append(target)
            if len(calls) == 1:
                raise PermissionError("reader holds state file")
            original(source, target)
        with patch("magic_converter.core.engines.office_com.os.replace", side_effect=locked_once):
            _save_json(path, {"stage": "saving"})
        self.assertEqual(json.loads(path.read_text(encoding="utf-8"))["stage"], "saving")
        self.assertEqual(len(calls), 2)

    def test_sheet_and_slide_models_keep_pictures_and_merged_tables(self):
        image = self.root / "model-image.png"
        Image.new("RGB", (30, 20), "green").save(image)
        source = self.root / "model-sheet.xlsx"
        wb = Workbook()
        wb.active.merge_cells("A1:B1")
        wb.active["A1"] = "merged"
        from openpyxl.drawing.image import Image as SheetImage
        wb.active.add_image(SheetImage(image), "A3")
        wb.save(source)
        output = self.root / "model-sheet.hwpx"
        convert_hwp(source, output, lambda _: None)
        restored = read_hwpx(output)
        self.assertEqual(len(inventory(restored)["pictures"]), 1)
        table = next(b for b in restored.blocks if isinstance(b, Table))
        self.assertEqual(table.cells[0].colspan, 2)
        deck = Presentation()
        slide = deck.slides.add_slide(deck.slide_layouts[6])
        slide.shapes.add_picture(str(image), Inches(1), Inches(1))
        table = slide.shapes.add_table(2, 2, Inches(1), Inches(2), Inches(3), Inches(1)).table
        table.cell(0, 0).merge(table.cell(0, 1))
        source = self.root / "model-slide.pptx"
        deck.save(source)
        output = self.root / "model-slide.hwpx"
        convert_hwp(source, output, lambda _: None)
        restored = read_hwpx(output)
        self.assertEqual(len(inventory(restored)["pictures"]), 1)
        table = next(b for b in restored.blocks if isinstance(b, Table))
        self.assertEqual(table.cells[0].colspan, 2)

    def test_variant_recipe_runs_actual_hwp_and_keeps_warnings_in_history(self):
        from magic_converter.core.settings.history import HistoryStore
        source = self.root / "recipe.docx"
        write_docx(self.fixture(), source)
        source = self.variant(source, ".docm")
        catalog = create_catalog(rhwp_available=True)
        request = prepare_request([source], "document:hwp", catalog)
        worker = ConversionWorker(51, request.paths, request.action, None, request=request)
        results = []
        worker.signals.finished.connect(results.append)
        worker.run()
        self.assertFalse(results[0].errors)
        validate_output(results[0].outputs[0])
        self.assertTrue(any("매크로" in message for message in results[0].warnings))
        history = HistoryStore(self.root / "history.json")
        history.append(results[0])
        self.assertIn("매크로", json.dumps(HistoryStore(history.path).entries, ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
