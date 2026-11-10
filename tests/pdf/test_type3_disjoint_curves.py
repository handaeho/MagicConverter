import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import pymupdf as fitz
from pptx import Presentation

from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import type3_disjoint_curve_pdf
from magic_converter.core.jobs.job_control import JobCancelled, JobTimedOut
from magic_converter.core.pdf.pdf_type3_simple_curves import simple_curve_items


class DisjointCurveTests(unittest.TestCase):
    def convert(self, folder, case):
        source=Path(folder)/'source.pdf'
        type3_disjoint_curve_pdf(source,case)
        target=source.with_suffix('.pptx')
        pdf_to_pptx_editable(source,target)
        shapes=Presentation(target).slides[0].shapes
        self.assertIn('Independent title',[s.text for s in shapes if s.has_text_frame])
        return shapes

    def test_disjoint_concave_contours_keep_cubics_and_uniform_alpha(self):
        for case,cubics in [('disjoint',2),('reversed',2),('mixed',1),('masked',2)]:
            with self.subTest(case=case),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,case)
                self.assertFalse(any(s.shape_type==13 for s in shapes))
                group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
                self.assertEqual(len(group.shapes),1)
                element=group.shapes[0]._element
                self.assertEqual(len(element.xpath('.//a:moveTo')),2)
                self.assertEqual(len(element.xpath('.//a:cubicBezTo')),cubics)
                if case=='masked':
                    self.assertEqual(element.xpath('.//a:alpha')[0].get('val'),'50000')

    def test_touch_overlap_nesting_and_invalid_later_contour_keep_whole_replay(self):
        for case in ('touching','near-touching','overlap','crossing','gradient','partial'):
            with self.subTest(case=case),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,case)
                self.assertEqual(sum(s.shape_type==13 for s in shapes),1)
                self.assertFalse(any(s.name.startswith('PDF glyph outline region ')for s in shapes))

    def test_nested_nonconvex_ink_uses_nonzero_boundary_changes(self):
        for case,contours,cubics in [('nested',1,1),('nested-hole',2,1),('curved-hole',2,2),
                                     ('curved-same',1,1),('curved-island',3,2),('pocket',2,1)]:
            with self.subTest(case=case),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,case)
                self.assertFalse(any(s.shape_type==13 for s in shapes))
                group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
                self.assertEqual(len(group.shapes[0]._element.xpath('.//a:moveTo')),contours)
                self.assertEqual(len(group.shapes[0]._element.xpath('.//a:cubicBezTo')),cubics)

    def test_compound_budget_is_not_reset_for_each_contour(self):
        with tempfile.TemporaryDirectory()as folder,patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_OVERLAP_CHECKS',1200):
            shapes=self.convert(folder,'disjoint')
            self.assertEqual(sum(s.shape_type==13 for s in shapes),1)
            self.assertFalse(any(s.name.startswith('PDF glyph outline region ')for s in shapes))

    def test_proof_preserves_original_coordinates_and_order(self):
        point=fitz.Point
        contour=[('l',point(0,0),point(4,0)),('l',point(4,0),point(4,1)),
                 ('c',point(4,1),point(2,1),point(1,2),point(1,3)),
                 ('l',point(1,3),point(0,3)),('close',)]
        for delta in (point(10,0),point(0,10),point(-10,-10)):
            other=[(item[0],*(p+delta for p in item[1:]))for item in contour]
            for matrix in (fitz.Matrix(1,1),fitz.Matrix(90),fitz.Matrix(-1,1)):
                for offset in (point(0,0),point(-20,-30),point(1e5,1e5)):
                    items=[(item[0],*(p*matrix+offset for p in item[1:]))for item in contour+other]
                    with self.subTest(delta=delta,matrix=matrix,offset=offset):
                        self.assertIs(simple_curve_items(items,4096),items)

    def test_unclosed_empty_and_discontinuous_later_contours_are_rejected(self):
        p=fitz.Point
        triangle=[('l',p(0,0),p(1,0)),('l',p(1,0),p(0,1)),('close',)]
        for tail in ([('close',)],[('l',p(10,0),p(11,0))],
                     [('l',p(10,0),p(11,0)),('l',p(12,0),p(10,1)),('close',)]):
            with self.subTest(tail=tail),self.assertRaises(ValueError):
                simple_curve_items(triangle+tail,4096)

    def test_cancellation_propagates_without_returning_partial_ink(self):
        for error in (JobCancelled(),JobTimedOut()):
            with self.subTest(error=type(error).__name__),patch(
                    'magic_converter.core.pdf.pdf_type3_simple_curves.check_cancelled',side_effect=error):
                with self.assertRaises(type(error)):
                    simple_curve_items([('close',)],4096)
