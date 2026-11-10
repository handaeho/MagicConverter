"""PDF baseline direction must survive imperfect recovered span quads."""
import math
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz
from pptx import Presentation

from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.engines.office_engine import find_office
from magic_converter.core.pdf.pdf_fonts import resolve_font
from magic_converter.core.pdf.pdf_layout import text_spans
from magic_converter.core.pdf.pdf_pptx_text import span_geometry


class TextDirectionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / 'source.pdf'
        self.target = self.root / 'editable.pptx'

    def tearDown(self):
        self.temporary.cleanup()

    def fixture(self, matrix, *, local_font=False):
        with fitz.open() as pdf:
            page = pdf.new_page(width=400, height=400)
            fontname = 'helv'
            if local_font:
                face = resolve_font('Arial')
                if face is None:
                    self.skipTest('Arial is not installed')
                fontname = 'local'
                page.insert_font(fontname=fontname, fontfile=str(face.path))
            page.insert_text((80, 260), 'Horizontal 0123', fontname=fontname, fontsize=24,
                             morph=(fitz.Point(80, 260), matrix))
            span = text_spans(page)[0]
            pdf.save(self.source)
        return span

    def test_horizontal_source_never_gets_rotation_from_recovered_quad(self):
        # A small, real x/y scale difference makes recover_quad's upper edge
        # slope by ~0.03 degrees even though the PDF baseline is horizontal.
        span = self.fixture(fitz.Matrix(1.004, 1))
        self.assertEqual(span.direction, (1, 0))
        self.assertGreater(abs(span.quad[0][1] - span.quad[1][1]), .01)
        self.assertEqual(span_geometry(span)[4], 0)
        pdf_to_pptx_editable(self.source, self.target)
        shapes = [s for s in Presentation(self.target).slides[0].shapes if s.has_text_frame]
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0].rotation, 0)
        self.assertIn('Horizontal', shapes[0].text)

    def test_true_small_oblique_and_right_angle_rotations_remain_editable(self):
        # No threshold snapping: genuine small rotations must stay rotated.
        for angle in (.02, 30, 90, -20):
            with self.subTest(angle=angle):
                span = self.fixture(fitz.Matrix(angle))
                expected = math.degrees(math.atan2(span.direction[1], span.direction[0])) % 360
                self.assertGreater(min(expected, 360 - expected), .01)
                self.assertAlmostEqual(span_geometry(span)[4], expected, delta=1 / 60000)
                pdf_to_pptx_editable(self.source, self.target)
                shapes = [s for s in Presentation(self.target).slides[0].shapes if s.has_text_frame]
                self.assertEqual(len(shapes), 1)
                self.assertAlmostEqual(shapes[0].rotation, expected, delta=1 / 60000)

    @unittest.skipUnless(find_office('PowerPoint'), 'Desktop PowerPoint is not installed')
    def test_native_export_keeps_horizontal_text_searchable_and_positioned(self):
        from magic_converter.core.engines.hwp_converter import run_com
        self.fixture(fitz.Matrix(1.004, 1), local_font=True)
        pdf_to_pptx_editable(self.source, self.target)
        output = self.root / 'native.pdf'
        run_com(self.target, output, 'office_pdf')
        with fitz.open(self.source) as source, fitz.open(output) as native:
            def characters(page):
                return [c for span in text_spans(page) for c in span.characters if not c.text.isspace()]
            before, after = characters(source[0]), characters(native[0])
            self.assertEqual([c.text for c in after], [c.text for c in before])
            for a, b in zip(before, after):
                self.assertAlmostEqual(a.origin[0], b.origin[0], delta=.6)
                self.assertAlmostEqual(a.origin[1], b.origin[1], delta=.6)
        deck = Presentation(self.target)
        deck.slides[0].shapes[0].text = 'Still editable'
        deck.save(self.target)
        self.assertEqual(Presentation(self.target).slides[0].shapes[0].text, 'Still editable')


if __name__ == '__main__':
    unittest.main()
