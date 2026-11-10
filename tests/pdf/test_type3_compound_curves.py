import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pptx import Presentation
from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import type3_compound_curve_pdf


class CompoundCurveTests(unittest.TestCase):
    def convert(self, folder, case):
        source=Path(folder)/'source.pdf';type3_compound_curve_pdf(source,case)
        target=source.with_suffix('.pptx');pdf_to_pptx_editable(source,target)
        shapes=Presentation(target).slides[0].shapes
        self.assertIn('Independent title',[s.text for s in shapes if s.has_text_frame])
        return shapes

    def test_proven_curved_holes_islands_and_disjoint_ink_retain_cubics(self):
        for case,contours,cubics in [('hole',2,8),('same-winding',1,4),('island',3,12),('disjoint',2,8),('mixed',2,4)]:
            with self.subTest(case=case),tempfile.TemporaryDirectory() as folder:
                shapes=self.convert(folder,case)
                self.assertFalse(any(s.shape_type==13 for s in shapes))
                group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
                self.assertEqual(len(group.shapes),1)
                element=group.shapes[0]._element
                self.assertEqual(len(element.xpath('.//a:moveTo')),contours)
                self.assertEqual(len(element.xpath('.//a:cubicBezTo')),cubics)

    def test_touch_overlap_unproven_nesting_mask_and_partial_clip_keep_replay(self):
        for case in ('touching','overlap','near-touching','masked-gradient','partial'):
            with self.subTest(case=case),tempfile.TemporaryDirectory() as folder:
                shapes=self.convert(folder,case)
                self.assertEqual(sum(s.shape_type==13 for s in shapes),1)
                self.assertFalse(any(s.name.startswith('PDF glyph outline region ')for s in shapes))

    def test_curve_topology_budget_never_publishes_partial_ink(self):
        with tempfile.TemporaryDirectory() as folder,patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_OVERLAP_CHECKS',1):
            self.assertEqual(sum(s.shape_type==13 for s in self.convert(folder,'island')),1)
