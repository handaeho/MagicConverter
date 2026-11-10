"""Real cell grids, pictures and scan limits through the worker dispatcher."""

import hashlib
import io
import os
from pathlib import Path
import tempfile
import unittest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import pymupdf as fitz
from PIL import Image
from docx import Document
from pptx import Presentation

from magic_converter.core.conversion.extended_converter import convert_extended
from magic_converter.core.conversion.file_router import ALL_ACTIONS
from magic_converter.core.pdf.pdf_layout import extract_tables, text_spans, font_name


class PdfStructureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "merged tables and images.pdf"
        image = io.BytesIO()
        Image.new("RGBA", (60, 30), (230, 120, 20, 128)).save(image, "PNG")
        document = fitz.open()
        page = document.new_page(width=480, height=360)
        page.draw_rect(page.rect, color=None, fill=(.94, .97, .99))
        page.insert_text((40, 50), "Table and pictures", fontsize=16)
        page.draw_rect((40, 100, 400, 140), color=None, fill=(.8, .9, 1))
        for y in (100, 140, 220):
            page.draw_line((40, y), (400, y), color=(.1, .2, .3), width=.6)
        page.draw_line((40, 180), (260, 180), color=(.1, .2, .3), width=.6)
        for x in (40, 260, 400):
            page.draw_line((x, 100), (x, 220), color=(.1, .2, .3), width=.6)
        page.draw_line((140, 140), (140, 220), color=(.1, .2, .3), width=.6)
        for x, y, text in ((50, 125, "Combined header"), (270, 125, "Value"),
                           (50, 165, "Alpha"), (150, 165, "15"), (270, 165, "Vertical merge"),
                           (50, 205, "Beta")):
            page.insert_text((x, y), text, fontsize=10, fontname="hebo" if y == 125 else "helv")
        page.insert_image((165, 190, 225, 215), stream=image.getvalue())
        page.insert_image((40, 270, 130, 315), stream=image.getvalue())
        page.insert_text((160, 295), "Outside picture caption", fontsize=11)
        document.save(self.source)
        document.close()
        self.sha256 = hashlib.sha256(self.source.read_bytes()).hexdigest()

    def tearDown(self):
        self.temporary.cleanup()

    def convert(self, key):
        progress, notices = [], []
        output = convert_extended(self.source, ALL_ACTIONS[key], progress.append, notices.append)
        self.assertEqual(progress[-1], 100)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).hexdigest(), self.sha256)
        return output, notices

    def test_docx_has_real_merged_table_and_two_pictures(self):
        path, notices = self.convert("document:docx")
        doc = Document(path)
        self.assertFalse(notices)
        self.assertEqual(len(doc.tables), 1)
        table = doc.tables[0]
        self.assertEqual((len(table.rows), len(table.columns)), (3, 3))
        self.assertEqual(table.cell(0, 0)._tc.grid_span, 2)
        self.assertIs(table.cell(1, 2)._tc, table.cell(2, 2)._tc)
        self.assertEqual(table.cell(0, 0).text, "Combined header")
        self.assertEqual(table.cell(1, 0).text, "Alpha")
        self.assertEqual(table.cell(1, 1).text, "15")
        self.assertEqual(len(doc.inline_shapes), 2)
        self.assertIn("Outside picture caption", "\n".join(p.text for p in doc.paragraphs))
        table.cell(1, 1).text = "Edited 42"
        edited = self.root / "edited.docx"
        doc.save(edited)
        self.assertEqual(Document(edited).tables[0].cell(1, 1).text, "Edited 42")
        self.assertEqual(len(Document(edited).inline_shapes), 2)

    def test_pptx_has_editable_cells_merges_styles_and_no_raster_table_ghost(self):
        path, notices = self.convert("pptx_editable")
        deck = Presentation(path)
        self.assertFalse(notices)
        slide = deck.slides[0]
        shapes = [shape for shape in slide.shapes if shape.has_table]
        self.assertEqual(len(shapes), 1)
        shape, table = shapes[0], shapes[0].table
        self.assertEqual((len(table.rows), len(table.columns)), (3, 3))
        self.assertTrue(table.cell(0, 0).is_merge_origin)
        self.assertEqual(table.cell(0, 0).span_width, 2)
        self.assertEqual(table.cell(1, 2).span_height, 2)
        self.assertEqual(table.cell(1, 1).text, "15")
        self.assertAlmostEqual(shape.left.pt, 40, delta=.05)
        self.assertAlmostEqual(shape.top.pt, 100, delta=.05)
        self.assertTrue(all(abs(a - b) <= 1 for a, b in
                            zip(table.cell(0, 0).fill.fore_color.rgb, (204, 230, 255))))
        # The cell's picture remains as a movable picture above the native grid.
        self.assertEqual(sum(s.shape_type == 13 for s in slide.shapes), 2)
        self.assertFalse(any(s.shape_type in {1, 5, 9} for s in slide.shapes),
                         "Moving the table must not leave separate copies of its original rules")
        # Neither a full-page background nor a table-sized raster copy survives.
        table_bounds = fitz.Rect(shape.left.pt, shape.top.pt, shape.left.pt + shape.width.pt, shape.top.pt + shape.height.pt)
        self.assertFalse(any(fitz.Rect(p.left.pt, p.top.pt, p.left.pt + p.width.pt, p.top.pt + p.height.pt).contains(table_bounds)
                             for p in slide.shapes if p.shape_type == 13))
        # Cell text must not be duplicated as independent textboxes as well.
        self.assertFalse(any(s.has_text_frame and "Alpha" in s.text for s in slide.shapes))
        table.cell(1, 1).text = "Edited 42"
        edited = self.root / "edited.pptx"
        deck.save(edited)
        reopened = Presentation(edited)
        self.assertEqual(next(s.table for s in reopened.slides[0].shapes if s.has_table).cell(1, 1).text, "Edited 42")

    def test_scanned_page_is_retained_and_explicitly_not_claimed_editable(self):
        with fitz.open(self.source) as source:
            png = source[0].get_pixmap(dpi=150).tobytes("png")
        document = fitz.open()
        page = document.new_page(width=480, height=360)
        page.insert_image(page.rect, stream=png)
        scanned = self.root / "scan.pdf"
        document.save(scanned)
        document.close()
        for key in ("document:docx", "pptx_editable"):
            with self.subTest(key=key):
                notices = []
                output = convert_extended(scanned, ALL_ACTIONS[key], lambda _: None, notices.append)
                self.assertTrue(any("OCR" in notice for notice in notices))
                if key == "document:docx":
                    doc = Document(output)
                    self.assertFalse(doc.tables)
                    self.assertEqual(len(doc.inline_shapes), 1)
                else:
                    self.assertFalse(any(s.has_table for s in Presentation(output).slides[0].shapes))

    def test_ruled_blank_tables_are_editable_and_pdf_font_names_are_repaired(self):
        document = fitz.open()
        page = document.new_page(width=480, height=360)
        for x in (40, 160, 280):
            page.draw_line((x, 40), (x, 160))
        for y in (40, 100, 160):
            page.draw_line((40, y), (280, y))
        blank = self.root / "blank.pdf"
        document.save(blank)
        document.close()
        for key in ("document:docx", "pptx_editable"):
            output = convert_extended(blank, ALL_ACTIONS[key], lambda _: None)
            if key == "document:docx":
                self.assertEqual(len(Document(output).tables), 1)
            else:
                self.assertEqual(sum(s.has_table for s in Presentation(output).slides[0].shapes), 1)
        self.assertEqual(font_name("ABCDEF+휴먼명조"), "Batang")


if __name__ == "__main__":
    unittest.main()
