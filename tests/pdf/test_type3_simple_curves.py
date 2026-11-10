import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from pptx import Presentation
from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import clipped_sequence_pdf

PROGRAM=(b'600 0 0 0 600 700 d1 0 0 m 600 0 l 600 250 l 400 250 l '
         b'300 250 300 350 300 400 c 300 700 l 0 700 l h f')


class SimpleCurveTests(unittest.TestCase):
    def shapes(self,folder,program=PROGRAM,mask=False):
        p=Path(folder)/'source.pdf';clipped_sequence_pdf(p,'0 0 360 240',type3=True,mask=mask,type3_program=program)
        target=p.with_suffix('.pptx');pdf_to_pptx_editable(p,target);return Presentation(target).slides[0].shapes
    def test_single_nonconvex_curve_keeps_original_curve_and_can_have_uniform_mask(self):
        for mask in(False,True):
            with self.subTest(mask=mask),tempfile.TemporaryDirectory()as folder:
                shapes=self.shapes(folder,mask=mask);self.assertFalse(any(s.shape_type==13 for s in shapes))
                group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
                self.assertEqual(len(group.shapes[0]._element.xpath('.//a:cubicBezTo')),1)
    def test_crossing_and_nonmonotone_curves_and_low_budget_keep_replay(self):
        for program in (PROGRAM.replace(b'300 250 300 350',b'900 900 -300 900'),
                        PROGRAM.replace(b'300 700 l 0 700 l',b'0 0 l 0 700 l 600 0 l')):
            with self.subTest(program=program),tempfile.TemporaryDirectory()as folder:
                self.assertEqual(sum(s.shape_type==13 for s in self.shapes(folder,program)),1)
        with tempfile.TemporaryDirectory()as folder,patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_OVERLAP_CHECKS',1):
            self.assertEqual(sum(s.shape_type==13 for s in self.shapes(folder)),1)
