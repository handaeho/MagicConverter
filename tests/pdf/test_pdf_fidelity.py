"""Regression evidence for PDF layout panels, font names and real Office output."""

import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image, ImageChops, ImageStat
from pptx import Presentation

from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.pdf.pdf_fonts import resolve_font, font_metrics
from magic_converter.core.pdf.pdf_layout import text_spans
from magic_converter.core.engines.office_engine import find_office


class PdfFidelityTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source, self.output = self.root / "source.pdf", self.root / "result.pptx"

    def tearDown(self):
        self.temporary.cleanup()

    def fixture(self):
        document = fitz.open()
        page = document.new_page(width=360, height=240)
        page.draw_rect(page.rect, color=None, fill=(.9, .95, 1))
        page.insert_text((30, 70), "Large blue", fontname="hebo", fontsize=18, color=(0, .2, .5))
        page.insert_text((135, 70), "small black", fontsize=11)
        # TJ-like positioning, with no U+0020 between C and D in the content.
        for x, c in zip((30, 37, 44, 57, 64, 71), "ABCDEF"):
            page.insert_text((x, 130), c, fontsize=12)
        page.insert_text((330, 210), "Rotated", fontsize=12, rotate=90)
        document.save(self.source)
        document.close()

    def test_layout_panels_are_not_promoted_to_a_table(self):
        document = fitz.open()
        page = document.new_page(width=480, height=360)
        page.draw_rect(page.rect, color=(.2, .2, .2))
        page.draw_line((160, 0), (160, 360))
        page.draw_rect((0, 20, 480, 50), color=None, fill=(.8, .8, .8))
        page.draw_rect((20, 100, 140, 160), color=None, fill=(.8, .9, 1))
        page.insert_text((30, 120), "Overview", fontsize=12)
        page.insert_text((30, 150), "Description", fontsize=10)
        expected = page.get_pixmap(dpi=200).tobytes("png")
        document.save(self.source)
        document.close()
        pdf_to_pptx_editable(self.source, self.output)
        slide = Presentation(self.output).slides[0]
        self.assertFalse(any(s.has_table for s in slide.shapes))
        self.assertEqual([s.text for s in slide.shapes if s.has_text_frame and s.text], ["Overview", "Description"])
        panels = [s for s in slide.shapes if s.shape_type == 1 and s.top.pt >= 19]
        self.assertEqual(len(panels), 2)
        self.assertEqual(sum(s.shape_type == 13 for s in slide.shapes), 0)

    def test_unknown_font_without_outline_keeps_original_pixels_instead_of_substituting(self):
        self.fixture()
        before = hashlib.sha256(self.source.read_bytes()).digest()
        notices = []
        with fitz.open(self.source) as pdf:
            expected = pdf[0].get_pixmap(dpi=200).tobytes("png")
        with (patch("magic_converter.core.pdf.pdf_pptx_text.resolve_font", return_value=None),
              patch("magic_converter.core.pdf.pdf_pptx_glyphs.m.fz_outline_glyph", side_effect=RuntimeError("No vector outline"))):
            pdf_to_pptx_editable(self.source, self.output, warning=notices.append)
        slide = Presentation(self.output).slides[0]
        self.assertGreaterEqual(len(slide.shapes), 3)
        self.assertTrue(all(s.shape_type == 13 for s in slide.shapes))
        self.assertTrue(notices)
        self.assertEqual(before, hashlib.sha256(self.source.read_bytes()).digest())
        with Image.open(io.BytesIO(expected)) as original:
            canvas = Image.new("RGBA", original.size, tuple(slide.background.fill.fore_color.rgb) + (255,))
            for picture in slide.shapes:
                with Image.open(io.BytesIO(picture.image.blob)) as ink:
                    canvas.alpha_composite(ink, (round(picture.left.pt * 200 / 72), round(picture.top.pt * 200 / 72)))
            self.assertLess(sum(ImageStat.Stat(ImageChops.difference(canvas.convert("RGB"), original.convert("RGB"))).mean) / 3, .5)

    def test_pdf_postscript_name_resolves_to_an_office_family(self):
        regular, bold = resolve_font("Helvetica"), resolve_font("Helvetica-Bold", True)
        if regular is None or bold is None:
            self.skipTest("Arial is not installed")
        self.assertEqual(regular.family, "Arial")
        self.assertFalse(regular.bold)
        self.assertTrue(bold.bold)
        self.assertGreater(font_metrics(bold).text_length("Editable", fontsize=18), 0)

    def test_uneditable_neighbour_keeps_its_own_layer_without_erasing_editable_text(self):
        from magic_converter.core.pdf.pdf_pptx_text import editable_font
        document = fitz.open()
        page = document.new_page(width=360, height=240)
        page.insert_text((30, 70), "Overlap", fontname="hebo", fontsize=18)
        page.insert_text((70, 70), "Fallback", fontsize=10)
        expected = page.get_pixmap(dpi=200).tobytes("png")
        document.save(self.source)
        document.close()
        with patch("magic_converter.core.pdf.pdf_pptx_objects.editable_font",
                   side_effect=lambda span: None if "Fallback" in span.text else editable_font(span)):
            pdf_to_pptx_editable(self.source, self.output)
        slide = Presentation(self.output).slides[0]
        self.assertEqual(len(slide.shapes), 2)
        self.assertEqual(slide.shapes[0].text, "Overlap")
        self.assertEqual(slide.shapes[1].shape_type, 6)
        self.assertTrue(slide.shapes[1].name.startswith('PDF glyph outline region '))
        self.assertEqual(len(slide.shapes[1].shapes),1)

    @unittest.skipUnless(find_office("PowerPoint"), "Desktop PowerPoint is not installed")
    def test_powerpoint_preserves_character_positions_and_mixed_size_baselines(self):
        from magic_converter.core.engines.hwp_converter import convert_office_pdf
        self.fixture()
        pdf_to_pptx_editable(self.source, self.output)
        rendered = self.root / "powerpoint.pdf"
        convert_office_pdf(self.output, rendered, lambda _: None)
        with fitz.open(self.source) as original, fitz.open(rendered) as result:
            def characters(page):
                return [(c.text, c.origin) for span in text_spans(page) for c in span.characters if not c.text.isspace()]
            expected, actual = characters(original[0]), characters(result[0])
            self.assertEqual([c for c, _ in expected], [c for c, _ in actual])
            for (character, (x, y)), (_, (a, b)) in zip(expected, actual):
                with self.subTest(character=character, position=(x, y)):
                    self.assertAlmostEqual(x, a, delta=.6)
                    self.assertAlmostEqual(y, b, delta=.6)
        deck = Presentation(self.output)
        for shape in deck.slides[0].shapes:
            if shape.has_text_frame:
                self.assertFalse(shape.text_frame.word_wrap)
        # The result remains an ordinary editable textbox after save/reopen.
        editable = next(s for s in deck.slides[0].shapes if s.has_text_frame)
        editable.text_frame.paragraphs[0].runs[0].text = "Edited"
        deck.save(self.output)
        self.assertTrue(any(s.has_text_frame and "Edited" in s.text for s in Presentation(self.output).slides[0].shapes))


if __name__ == "__main__":
    unittest.main()
