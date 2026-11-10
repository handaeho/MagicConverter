"""Keep opaque overlapping glyphs separate from Office compound fills."""
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from pptx import Presentation

from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import opaque_overlap_pdf


class OpaqueGlyphOverlapTests(unittest.TestCase):
    def convert(self, directory, **options):
        source = Path(directory) / 'source.pdf'
        opaque_overlap_pdf(source, **options)
        target = source.with_suffix('.pptx')
        pdf_to_pptx_editable(source, target)
        shapes = Presentation(target).slides[0].shapes
        self.assertEqual([s.text for s in shapes if s.has_text_frame], ['Independent title'])
        return shapes

    def test_opaque_overlap_keeps_each_glyph_paint_and_counters(self):
        for text in ('WWWW', 'OOOO'):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as folder:
                shapes = self.convert(folder, text=text)
                groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
                self.assertEqual(len(groups), 1)
                self.assertEqual(len(groups[0].shapes), 4)
                self.assertFalse(any(s.shape_type == 13 for s in shapes))

    def test_separated_and_translucent_controls(self):
        for options, expected in (({'overlap': False}, 1), ({'opacity': .5}, 4)):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as folder:
                shapes = self.convert(folder, **options)
                group = next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
                self.assertEqual(len(group.shapes), expected)
                alpha = [int(a.get('val')) for s in group.shapes for a in s._element.xpath('.//a:alpha')]
                self.assertEqual(alpha, [50000] * 4 if options.get('opacity') else [])

    def test_clip_and_outline_budget_keep_complete_replay(self):
        for clipped, exhausted in ((True, False), (False, True)):
            with self.subTest(clipped=clipped), tempfile.TemporaryDirectory() as folder:
                with patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_GLYPHS', 1 if exhausted else 1024):
                    shapes = self.convert(folder, clipped=clipped)
                self.assertEqual(sum(s.shape_type == 13 for s in shapes), 1)
                self.assertFalse(any(s.name.startswith('PDF glyph outline region ') for s in shapes))

    def test_overlapping_boxes_without_intersecting_ink_keep_compound_path(self):
        with tempfile.TemporaryDirectory() as folder:
            shapes = self.convert(folder, nested_counter=True)
            group = next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
            self.assertEqual(len(group.shapes), 1)

    def test_ink_overlap_comparison_budget_keeps_complete_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_OVERLAP_CHECKS', 0):
                shapes = self.convert(folder)
            self.assertEqual(sum(s.shape_type == 13 for s in shapes), 1)
            self.assertFalse(any(s.name.startswith('PDF glyph outline region ') for s in shapes))

    def test_small_kerning_repairs_holes_without_changing_original_edges(self):
        with tempfile.TemporaryDirectory() as folder:
            with patch('magic_converter.core.pdf.pdf_pptx_glyphs._filled_ink_overlaps', return_value=(False, [])):
                baseline = self.convert(folder, tracking=-12)
            original = next(s for s in baseline if s.name.startswith('PDF glyph outline region ')).shapes[0]
            shapes = self.convert(folder, tracking=-12)
            group = next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
            self.assertEqual(len(group.shapes), 4)  # compound and three intersections
            self.assertEqual(original._element.spPr.xml, group.shapes[0]._element.spPr.xml)
            transforms = [s._element.spPr.xfrm.xml for s in group.shapes]
            self.assertTrue(all(t == transforms[0] for t in transforms))

    def test_small_intersection_budget_cannot_emit_partial_repairs(self):
        from magic_converter.core.pdf.pdf_pptx_glyphs import _intersection_items, _UnsupportedOutline
        from PySide6.QtGui import QPainterPath
        path = QPainterPath(); path.addRect(0, 0, 1, 1)
        with self.assertRaises(_UnsupportedOutline):
            _intersection_items(path, [1])

    def test_subpoint_intersection_keeps_existing_antialias_edges(self):
        import pymupdf as fitz
        from magic_converter.core.pdf.pdf_pptx_glyphs import _filled_ink_overlaps
        a, b = fitz.Rect(0, 0, 10, 10), fitz.Rect(9.9, 0, 19.9, 10)
        split, repairs = _filled_ink_overlaps([[('re', a, 1)], [('re', b, 1)]], [a, b])
        self.assertFalse(split); self.assertEqual(repairs, [])

    def test_polygonal_subpoint_hole_is_repaired_but_curved_control_is_unchanged(self):
        import pymupdf as fitz
        from PySide6.QtGui import QPainterPath
        from magic_converter.core.pdf.pdf_pptx_glyphs import _filled_ink_overlaps, _intersection_items
        a, b = fitz.Rect(0, 0, 10, 10), fitz.Rect(9.5, 0, 19.5, 10)
        split, repairs = _filled_ink_overlaps([[('re', a, 1)], [('re', b, 1)]], [a, b])
        self.assertFalse(split); self.assertEqual(len(repairs), 1)
        ellipses = []
        for box in (a, b):
            path = QPainterPath(); path.addEllipse(box.x0, box.y0, box.width, box.height)
            ellipses.append(_intersection_items(path, [30000]))
        split, repairs = _filled_ink_overlaps(ellipses, [a, b])
        self.assertFalse(split); self.assertEqual(repairs, [])


if __name__ == '__main__':
    unittest.main()
