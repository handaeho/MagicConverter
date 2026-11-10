import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from pptx import Presentation
from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import type3_compound_curve_pdf


class RefinedCurveTests(unittest.TestCase):
    def convert(self,folder,case):
        source=Path(folder)/'source.pdf';type3_compound_curve_pdf(source,case)
        target=source.with_suffix('.pptx');pdf_to_pptx_editable(source,target)
        shapes=Presentation(target).slides[0].shapes
        self.assertIn('Independent title',[s.text for s in shapes if s.has_text_frame])
        return shapes

    def test_refined_bounds_prove_wide_holes_and_nested_lenses_without_flattening(self):
        for name,cubics in [('ambiguous',8),('refined-same-winding',4),('nested-lens',6)]:
            with self.subTest(case=name),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,name)
                self.assertFalse(any(s.shape_type==13 for s in shapes))
                group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
                self.assertEqual(len(group.shapes[0]._element.xpath('.//a:cubicBezTo')),cubics)

    def test_touching_near_touch_overlap_mask_and_partial_clip_keep_complete_replay(self):
        for name in ('touching','near-touching','overlap','masked-gradient','partial'):
            with self.subTest(case=name),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,name)
                self.assertEqual(sum(s.shape_type==13 for s in shapes),1)
                self.assertFalse(any(s.name.startswith('PDF glyph outline region ')for s in shapes))

    def test_refinement_budget_failure_cannot_publish_a_partial_hole(self):
        with tempfile.TemporaryDirectory()as folder,patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_OVERLAP_CHECKS',600):
            self.assertEqual(sum(s.shape_type==13 for s in self.convert(folder,'ambiguous')),1)
