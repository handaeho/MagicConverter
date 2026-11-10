import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from pptx import Presentation
from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import type3_topology_pdf


class Type3TopologyTests(unittest.TestCase):
    def shapes(self,folder,case):
        source=Path(folder)/'source.pdf';type3_topology_pdf(source,case)
        target=source.with_suffix('.pptx');pdf_to_pptx_editable(source,target)
        shapes=Presentation(target).slides[0].shapes
        self.assertIn('Independent title',[s.text for s in shapes if s.has_text_frame])
        return shapes

    def test_simple_concave_holes_islands_and_disjoint_contours_keep_editable_ink(self):
        for name,count in [('concave',1),('hole',2),('same-winding',1),('island',3),('disjoint',2),('masked',1)]:
            with self.subTest(name=name),tempfile.TemporaryDirectory()as folder:
                shapes=self.shapes(folder,name)
                self.assertFalse(any(s.shape_type==13 for s in shapes))
                group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
                self.assertEqual(len(group.shapes),1)
                self.assertEqual(len(group.shapes[0]._element.xpath('.//a:moveTo')),count)

    def test_touching_crossing_retraced_masks_and_partial_clips_keep_complete_replay(self):
        for name in('touching','crossing','retraced','partial'):
            with self.subTest(name=name),tempfile.TemporaryDirectory()as folder:
                shapes=self.shapes(folder,name)
                self.assertEqual(sum(s.shape_type==13 for s in shapes),1)
                self.assertFalse(any(s.name.startswith('PDF glyph outline region ')for s in shapes))

    def test_topology_budget_exhaustion_keeps_entire_original_paint(self):
        with tempfile.TemporaryDirectory()as folder,patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_OVERLAP_CHECKS',1):
            self.assertEqual(sum(s.shape_type==13 for s in self.shapes(folder,'hole')),1)

if __name__=='__main__':unittest.main()
