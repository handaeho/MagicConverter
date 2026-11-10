"""Real PDF/PPTX checks: style coordinates, text removal and retained graphics."""

import hashlib
import io
import os
import tempfile
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf as fitz
from PIL import Image, ImageChops
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.enum.dml import MSO_FILL_TYPE
from PySide6.QtCore import QPoint
from PySide6.QtWidgets import QApplication

from magic_converter.core.conversion.converter import ConversionWorker, _pdf_text_spans
from magic_converter.core.conversion.extended_converter import convert_extended
from magic_converter.core.conversion.file_router import ALL_ACTIONS, DOCUMENT_EXTENSIONS, slots_for
from magic_converter.ui.common.icons import icon_path, make_icon
from magic_converter.ui.shell.radial_wheel import RadialWheelWindow

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class PptxModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "styled document.pdf"
        self.backgrounds = []
        document = fitz.open()
        for index in range(2):
            page = document.new_page(width=360, height=240)
            page.draw_rect(page.rect, color=None, fill=(0.88, 0.94, 0.98))
            # These strokes deliberately cross text bbox areas, so a test
            # catches MuPDF's default vector-removal policy immediately.
            page.draw_rect(fitz.Rect(20, 25, 340, 150), color=(0.1, 0.3, 0.4), width=1)
            page.draw_line((20, 45), (340, 45), color=(0, 0.4, 0.2), width=1)
            page.draw_line((170, 25), (170, 150), color=(0.2, 0.2, 0.2), width=1)
            buffer = io.BytesIO()
            Image.new("RGB", (48, 48), (236, 130, 20)).save(buffer, "PNG")
            page.insert_image(fitz.Rect(245, 30, 325, 110), stream=buffer.getvalue())
            self.backgrounds.append(page.get_pixmap(dpi=200, alpha=False).tobytes("png"))
            page.insert_text((35, 55), f"Editable Title {index + 1}", fontname="hebo", fontsize=18, color=(0.8, 0.15, 0.1))
            page.insert_text((35, 105), "Blue body text", fontname="helv", fontsize=12, color=(0.05, 0.2, 0.75))
            page.insert_text((35, 190), "Italic note", fontname="heit", fontsize=11, color=(0.2, 0.2, 0.2))
        document.save(self.source)
        document.close()
        self.original_hash = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):
        self.temporary.cleanup()

    def convert(self, key):
        progress = []
        result = convert_extended(self.source, ALL_ACTIONS[key], progress.append)
        self.assertEqual(progress[-1], 100)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.original_hash)
        return result

    def test_router_exposes_two_modes_and_no_removed_format(self):
        slots = slots_for([self.source], "convert", ffmpeg_available=False, presentation_available=False)
        self.assertEqual(len(slots), 10)
        for key, label in (("pptx_image", "PPTX (고정)"), ("pptx_editable", "PPTX (편집)")):
            slot = next(slot for slot in slots if slot.action.key == key)
            self.assertEqual(slot.action.label, label)
            self.assertTrue(slot.enabled)
        self.assertNotIn(".pages", DOCUMENT_EXTENSIONS)
        self.assertFalse(any("PAGES" in slot.action.label for slot in slots))
        self.assertNotIn("document:pptx", ALL_ACTIONS)
        self.assertFalse(any("PAGES" in slot.action.label for slot in slots_for(["doc.docx"], "convert")))

    def test_fixed_mode_is_exact_200dpi_image_and_uses_slide_name(self):
        path = self.convert("pptx_image")
        self.assertEqual(path.name, "styled document_slide.pptx")
        deck = Presentation(path)
        self.assertEqual((deck.slide_width.pt, deck.slide_height.pt), (360, 240))
        self.assertEqual(len(deck.slides), 2)
        with fitz.open(self.source) as pdf:
            for index, slide in enumerate(deck.slides):
                self.assertEqual(len(slide.shapes), 1)
                shape = slide.shapes[0]
                self.assertEqual(shape.shape_type, MSO_SHAPE_TYPE.PICTURE)
                self.assertEqual((shape.left, shape.top), (0, 0))
                self.assertEqual((shape.width, shape.height), (deck.slide_width, deck.slide_height))
                expected = Image.open(io.BytesIO(pdf[index].get_pixmap(dpi=200, alpha=False).tobytes("png"))).convert("RGB")
                actual = Image.open(io.BytesIO(shape.image.blob))
                self.assertAlmostEqual(actual.info["dpi"][0], 200, delta=0.2)
                self.assertIsNone(ImageChops.difference(expected, actual.convert("RGB")).getbbox())
                expected.close()
                actual.close()
        self.assertEqual(self.convert("pptx_image").name, "styled document_slide(1).pptx")

    def test_editable_mode_restores_individual_graphics_and_text_styles(self):
        path = self.convert("pptx_editable")
        self.assertEqual(path.name, "styled document_editable.pptx")
        deck = Presentation(path)
        self.assertEqual(len(deck.slides), 2)
        with fitz.open(self.source) as pdf:
            for index, slide in enumerate(deck.slides):
                spans = _pdf_text_spans(pdf[index])
                texts = [shape for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.TEXT_BOX]
                graphics = [shape for shape in slide.shapes if shape.shape_type in {MSO_SHAPE_TYPE.AUTO_SHAPE, MSO_SHAPE_TYPE.LINE, MSO_SHAPE_TYPE.FREEFORM}]
                pictures = [shape for shape in slide.shapes if shape.shape_type == MSO_SHAPE_TYPE.PICTURE]
                self.assertEqual(len(texts), len(spans))
                self.assertEqual(len(graphics), 3)
                self.assertEqual(len(pictures), 1)
                self.assertAlmostEqual(pictures[0].width.pt, 80, delta=1)
                self.assertAlmostEqual(pictures[0].height.pt, 80, delta=1)
                # No whole-page picture should contain raster ghosts of shapes.
                self.assertFalse(any(p.width == deck.slide_width and p.height == deck.slide_height for p in pictures))
                for shape, span in zip(texts, spans):
                    self.assertTrue(shape.has_text_frame)
                    self.assertEqual(shape.text, span.text)
                    self.assertAlmostEqual(shape.left.pt, span.bbox[0], delta=0.001)
                    self.assertAlmostEqual(shape.top.pt, span.bbox[1], delta=0.001)
                    self.assertAlmostEqual(shape.width.pt, span.bbox[2] - span.bbox[0], delta=0.001)
                    self.assertAlmostEqual(shape.height.pt, span.bbox[3] - span.bbox[1], delta=0.001)
                    frame = shape.text_frame
                    self.assertFalse(frame.word_wrap)
                    self.assertEqual((frame.margin_left, frame.margin_right, frame.margin_bottom), (0, 0, 0))
                    self.assertEqual(shape.fill.type, MSO_FILL_TYPE.BACKGROUND)
                    for run in frame.paragraphs[0].runs:
                        self.assertAlmostEqual(run.font.size.pt, span.size, delta=0.01)
                        self.assertEqual(str(run.font.color.rgb), f"{span.color:06X}")
                        self.assertEqual(run.font.bold, bool(span.flags & 16))
                        self.assertEqual(run.font.italic, bool(span.flags & 2))
        self.assertEqual(self.convert("pptx_editable").name, "styled document_editable(1).pptx")

    def test_worker_dispatch_and_icon_and_wheel(self):
        for index, key in enumerate(("pptx_image", "pptx_editable")):
            worker = ConversionWorker(index + 1, (self.source,), ALL_ACTIONS[key], None)
            results = []
            worker.signals.finished.connect(results.append)
            worker.run()
            self.assertEqual(results[0].completed, 1)
            self.assertFalse(results[0].errors)
            self.assertEqual(len(Presentation(results[0].outputs[0]).slides), 2)
        self.assertEqual(icon_path().name, "magic_converter-tool.ico")
        self.assertFalse((icon_path().parent / "magic_converter.ico").exists())
        self.assertFalse(make_icon().isNull())
        wheel = RadialWheelWindow(lambda: True)
        wheel.show_for_drag("convert", QPoint(500, 500))
        wheel.paths = (self.source,)
        wheel._rebuild_slots()
        wheel._open_animation.setCurrentTime(150)
        wheel._set_hover(2)
        APP.processEvents()
        artifact_dir = Path(".test-artifacts")
        artifact_dir.mkdir(exist_ok=True)
        wheel.grab().save(str(artifact_dir / "pptx-modes-wheel.png"))
        make_icon().pixmap(128, 128).save(str(artifact_dir / "tool-icon.png"))
        wheel.dismiss()
        wheel.deleteLater()

    def test_rotated_and_scanned_pages_keep_their_visual_shape(self):
        with fitz.open(self.source) as source:
            source[0].set_rotation(90)
            source[1].set_rotation(90)
            source.save(self.root / "rotated.pdf")
        rotated = convert_extended(self.root / "rotated.pdf", ALL_ACTIONS["pptx_editable"], lambda value: None)
        deck = Presentation(rotated)
        self.assertEqual((deck.slide_width.pt, deck.slide_height.pt), (240, 360))
        self.assertTrue(any(shape.has_text_frame and abs(shape.rotation) > 1 for shape in deck.slides[0].shapes))
        document = fitz.open()
        page = document.new_page(width=360, height=240)
        page.insert_image(page.rect, stream=self.backgrounds[0])
        scanned = self.root / "scanned.pdf"
        document.save(scanned)
        document.close()
        output = convert_extended(scanned, ALL_ACTIONS["pptx_editable"], lambda value: None)
        self.assertEqual(len(Presentation(output).slides[0].shapes), 1)


if __name__ == "__main__":
    unittest.main()
