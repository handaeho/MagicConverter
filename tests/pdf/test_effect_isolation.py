import io
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz
from PIL import Image, ImageChops, ImageStat
from pptx import Presentation

from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import isolated_effect_pdf
from magic_converter.core.pdf.pdf_paint import PdfPaint


class EffectIsolationTests(unittest.TestCase):
    def test_nested_blend_respects_isolation_and_nonisolated_backdrop(self):
        with tempfile.TemporaryDirectory() as folder:
            for isolated in (True, False):
                with self.subTest(isolated=isolated):
                    source = Path(folder)/f'{isolated}.pdf'
                    isolated_effect_pdf(source, isolated=isolated)
                    with fitz.open(source) as pdf:
                        paint = PdfPaint(pdf[0])
                        self.assertEqual([(g.start, g.end) for g in paint.groups],
                                         [(2 if isolated else 0, 4)])
                    target = source.with_suffix('.pptx')
                    pdf_to_pptx_editable(source, target)
                    shapes = Presentation(target).slides[0].shapes
                    self.assertEqual(sum(s.shape_type == 1 for s in shapes), int(isolated))
                    self.assertEqual(sum(s.shape_type == 13 for s in shapes), 1)
                    self.assertEqual([s.text for s in shapes if s.has_text_frame and s.text],
                                     ['Independent title'] if isolated else [])

    def test_isolated_replacement_has_no_external_background_ink(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'isolated.pdf'
            isolated_effect_pdf(source)
            with fitz.open(source) as pdf:
                paint = PdfPaint(pdf[0])
                group, = paint.groups
                data, bounds = paint.render(range(group.start, group.end), pdf[0].rect, dpi=144)
                ink = Image.open(io.BytesIO(data))
                self.assertGreater(bounds.x0, 30)
                original = Image.open(io.BytesIO(pdf[0].get_pixmap(dpi=144, alpha=True).tobytes('png')))
                canvas = Image.new('RGBA', original.size)
                before, position = paint.render(range(group.start), pdf[0].rect, dpi=144)
                canvas.alpha_composite(Image.open(io.BytesIO(before)),
                                       (round(position.x0*2), round(position.y0*2)))
                canvas.alpha_composite(ink, (round(bounds.x0*2), round(bounds.y0*2)))
                self.assertLess(max(ImageStat.Stat(ImageChops.difference(original, canvas)).mean), .2)


if __name__ == '__main__':
    unittest.main()
