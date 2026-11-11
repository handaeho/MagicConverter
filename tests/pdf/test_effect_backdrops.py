"""A retained blend must not paint its translucent backdrop a second time."""
import io
from pathlib import Path
import tempfile
import unittest

import pymupdf as fitz
from PIL import Image, ImageChops, ImageStat
from pptx import Presentation

from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.engines.office_engine import find_office
from magic_converter.core.pdf.pdf_paint import PdfPaint


from magic_converter.core.diagnostics.pdf_fixtures import effect_pdf as fixture


class EffectBackdropTests(unittest.TestCase):
    def test_blend_consumes_its_backdrop_once_and_keeps_independent_text(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'effect.pdf'
            fixture(source)
            with fitz.open(source) as pdf:
                paint = PdfPaint(pdf[0])
                self.assertEqual([(g.start, g.end) for g in paint.groups], [(1, 3)])
                data, bounds = paint.render(range(1, 3), pdf[0].rect, dpi=144)
                # The replacement is the complete original connected region.
                reference = pdf[0].get_pixmap(dpi=144, alpha=True)
                ink = Image.open(io.BytesIO(data))
                original = Image.open(io.BytesIO(reference.tobytes('png')))
                canvas = Image.new('RGBA', original.size)
                canvas.alpha_composite(ink, (round(bounds.x0*2), round(bounds.y0*2)))
                area=(20*2, 80*2, 180*2, 190*2)
                self.assertLess(max(ImageStat.Stat(ImageChops.difference(original.crop(area),canvas.crop(area))).mean), .1)
            target = source.with_suffix('.pptx')
            pdf_to_pptx_editable(source, target)
            shapes = Presentation(target).slides[0].shapes
            self.assertEqual([s.text for s in shapes if s.has_text_frame], ['Independent before','Independent after'])
            self.assertEqual(sum(s.shape_type == 13 for s in shapes), 1)
            self.assertEqual(sum(s.shape_type == 1 for s in shapes), 0)

    def test_normal_group_keeps_its_independent_backdrop_editable(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'normal.pdf'; fixture(source, blend=False)
            pdf_to_pptx_editable(source, source.with_suffix('.pptx'))
            shapes = Presentation(source.with_suffix('.pptx')).slides[0].shapes
            self.assertEqual(sum(s.shape_type == 1 for s in shapes), 1)
            self.assertEqual(sum(s.shape_type == 13 for s in shapes), 1)

    def test_overlapping_blend_groups_share_one_backdrop(self):
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'overlap.pdf';fixture(source,repeat=True)
            with fitz.open(source) as pdf:
                paint=PdfPaint(pdf[0])
                self.assertEqual([(g.start,g.end) for g in paint.groups],[(1,4)])
            pdf_to_pptx_editable(source,source.with_suffix('.pptx'))
            shapes=Presentation(source.with_suffix('.pptx')).slides[0].shapes
            self.assertEqual(sum(s.shape_type==13 for s in shapes),1)
            self.assertEqual(sum(s.has_text_frame for s in shapes),2)

    @unittest.skipUnless(find_office('PowerPoint'), 'Desktop PowerPoint is not installed')
    def test_native_powerpoint_blend_and_deleting_the_effect_leave_no_ghost(self):
        from magic_converter.core.engines.hwp_converter import run_com
        from magic_converter.core.diagnostics.visual_comparison import compare
        with tempfile.TemporaryDirectory() as folder:
            source=Path(folder)/'effect.pdf'; fixture(source)
            deck=source.with_suffix('.pptx'); pdf_to_pptx_editable(source,deck)
            native=source.with_name('native.pdf'); run_com(deck,native,'office_pdf')
            result=compare(source,native)
            self.assertLess(result['pages'][0]['mae'],1)
            presentation=Presentation(deck)
            for shape in list(presentation.slides[0].shapes):
                if shape.shape_type == 13:
                    shape._element.getparent().remove(shape._element)
            presentation.save(deck); run_com(deck,native,'office_pdf')
            with fitz.open(native) as pdf:
                pix=pdf[0].get_pixmap()
                self.assertEqual(pix.pixel(80,110),(255,255,255))
                self.assertIn('Independent',pdf[0].get_text())


if __name__ == '__main__': unittest.main()
