"""Display-list culling must not flatten unrelated editable PDF content."""
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from PIL import Image, ImageChops, ImageStat
from pptx import Presentation

from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.pdf.pdf_paint import PdfPaint


from magic_converter.core.diagnostics.pdf_fixtures import clipped_sequence_pdf as clipped_pdf


class ClippedSequenceTests(unittest.TestCase):
    def test_one_edge_cubic_clip_retains_editable_curves_and_other_clips_replay(self):
        from magic_converter.core.diagnostics.pdf_fixtures import type3_curve_pdf
        for case in ('one-edge-top', 'one-edge-left', 'one-edge-masked', 'partial'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / 'input.pdf'
                target = source.with_suffix('.pptx')
                type3_curve_pdf(source, case)
                pdf_to_pptx_editable(source, target)
                shapes = Presentation(target).slides[0].shapes
                groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
                editable = case in {'one-edge-top', 'one-edge-left'}
                self.assertEqual(len(groups), int(editable))
                self.assertEqual(sum(s.shape_type == 13 for s in shapes), int(not editable))
                if editable:
                    self.assertEqual(len(groups[0].shapes[0]._element.xpath('.//a:cubicBezTo')), 4)

    def test_over_budget_retry_keeps_safe_whole_page_preservation(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.pdf'; clipped_pdf(source, '20 20 40 20')
            with patch('magic_converter.core.pdf.pdf_paint.MAX_PAGE_RETRY_PAINTS', 1):
                notes = []; pdf_to_pptx_editable(source, source.with_suffix('.pptx'), warning=notes.append)
            shapes = Presentation(source.with_suffix('.pptx')).slides[0].shapes
            self.assertEqual(len(shapes), 1)
            self.assertEqual(shapes[0].name, 'PDF preserved page (paint sequence mismatch)')
            self.assertTrue(any('편집 제한' in text for text in notes))

    def test_two_adjacent_edges_preserve_curves_and_mask_keeps_replay(self):
        from magic_converter.core.diagnostics.pdf_fixtures import type3_curve_pdf
        for name in ('two-edge-corner', 'two-edge-masked'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / 'input.pdf'
                target = source.with_suffix('.pptx')
                type3_curve_pdf(source, name)
                pdf_to_pptx_editable(source, target)
                shapes = Presentation(target).slides[0].shapes
                if name == 'two-edge-masked':
                    self.assertEqual(sum(shape.shape_type == 13 for shape in shapes), 1)
                else:
                    group, = (shape for shape in shapes if shape.name.startswith('PDF glyph outline region '))
                    glyph = group.shapes[0]
                    self.assertGreaterEqual(len(glyph._element.xpath('.//a:cubicBezTo')), 2)
                    self.assertTrue(glyph._element.xpath('.//a:lnTo'))
                    self.assertGreaterEqual(glyph.left.pt, 25.999)
                    self.assertGreaterEqual(glyph.top.pt, 103.999)

    def test_hidden_clip_does_not_flatten_independent_text(self):
        with tempfile.TemporaryDirectory() as folder:
            for clip in ('20 20 40 20', '20 20 0 0'):
                for type3 in (False, True):
                    with self.subTest(clip=clip, type3=type3):
                        source = Path(folder) / 'input.pdf'; target = source.with_suffix('.pptx')
                        clipped_pdf(source, clip, type3=type3)
                        notes = []; pdf_to_pptx_editable(source, target, warning=notes.append)
                        shapes = Presentation(target).slides[0].shapes
                        self.assertEqual([s.text for s in shapes if s.has_text_frame], ['Independent title'])
                        self.assertFalse(any(s.shape_type == 13 for s in shapes))
                        self.assertFalse(any('쪽 전체' in text for text in notes))

    def test_partial_clip_and_type3_keep_original_coverage(self):
        with tempfile.TemporaryDirectory() as folder:
            for clip in ('20 110 12 30', '0 0 360 240'):
                for type3 in (False, True):
                    with self.subTest(clip=clip, type3=type3):
                        source = Path(folder) / 'input.pdf'; clipped_pdf(source, clip, type3=type3)
                        with fitz.open(source) as pdf:
                            page = pdf[0]; paint = PdfPaint(page)
                            original = Image.open(io.BytesIO(page.get_pixmap(dpi=144, alpha=True).tobytes('png')))
                            blob, bounds = paint.render(range(len(paint.log)), page.rect, dpi=144)
                            canvas = Image.new('RGBA', original.size)
                            canvas.alpha_composite(Image.open(io.BytesIO(blob)), (round(bounds.x0*2), round(bounds.y0*2)))
                            self.assertIsNone(ImageChops.difference(original.getchannel('A'), canvas.getchannel('A')).getbbox())
                            self.assertLess(max(ImageStat.Stat(ImageChops.difference(original, canvas)).mean), .01)

    def test_single_convex_glyph_is_editable_and_stays_inside_exact_clip(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.pdf'
            target = source.with_suffix('.pptx')
            clipped_pdf(source, '20 114 12 24', type3=True)
            pdf_to_pptx_editable(source, target)
            shapes = Presentation(target).slides[0].shapes
            self.assertFalse(any(s.shape_type == 13 for s in shapes))
            group = next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
            self.assertEqual(len(group.shapes), 1)
            glyph = group.shapes[0]
            self.assertLessEqual((glyph.left + glyph.width) / 12700, 32.001)
            self.assertGreaterEqual(glyph.top.pt, 101.999)
            self.assertIn('Independent title', [s.text for s in shapes if s.has_text_frame])

    def test_partial_clip_keeps_two_subpaths_that_share_a_vertex_in_replay(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.pdf'
            target = source.with_suffix('.pptx')
            # Each subpath fills separately. Concatenating across the second
            # moveto invents a convex region between the two triangles.
            program = b'600 0 0 0 600 700 d1 0 200 m 250 0 l 600 200 l 600 200 m 550 700 l 100 650 l h f'
            clipped_pdf(source, '23 114 18 24', type3=True, type3_program=program)
            pdf_to_pptx_editable(source, target)
            shapes = Presentation(target).slides[0].shapes
            self.assertEqual(sum(s.shape_type == 13 for s in shapes), 1)
            self.assertFalse(any(s.name.startswith('PDF glyph outline region ') for s in shapes))

    def test_partial_clip_restores_disjoint_glyphs_and_keeps_unproven_paints_atomic(self):
        for program, expected in ((b'(I I I) Tj', 3), (b'[(I) 250 (I)] TJ', 0),
                                  (b'[(I) 178 (I)] TJ', 0), (b'(I O I) Tj', 0)):
            with self.subTest(program=program), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / 'input.pdf'
                target = source.with_suffix('.pptx')
                clipped_pdf(source, '22 111 62 27', text_program=b'/helv 40 Tf ' + program)
                pdf_to_pptx_editable(source, target)
                shapes = Presentation(target).slides[0].shapes
                groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
                self.assertEqual(sum(s.shape_type == 13 for s in shapes), int(not expected))
                self.assertEqual(len(groups), int(bool(expected)))
                if expected:
                    self.assertEqual(len(groups[0].shapes[0]._element.xpath('.//a:moveTo')), expected)
                self.assertIn('Independent title', [s.text for s in shapes if s.has_text_frame])

    def test_direct_retry_retains_mask_and_cancellation(self):
        from magic_converter.core.jobs.job_control import JobCancelled
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / 'input.pdf'
            clipped_pdf(source, '20 110 12 30', type3=True, mask=True)
            # A separate hidden ordinary paint triggers original-page retry;
            # the visible Type3/soft-mask paint must survive that same replay.
            with fitz.open(source) as pdf:
                page = pdf[0]; stream = pdf.get_new_xref(); pdf.update_object(stream, '<<>>')
                pdf.update_stream(stream, page.read_contents() +
                    b' q 0 0 1 1 re W n BT /helv 12 Tf 1 0 0 1 30 120 Tm (Hidden) Tj ET Q')
                page.set_contents(stream); pdf.save(source.with_name('masked.pdf'))
            source = source.with_name('masked.pdf')
            with fitz.open(source) as pdf:
                paint = PdfPaint(pdf[0])
                self.assertIsNotNone(paint._page)
                blob, _ = paint.render(range(len(paint.log)), pdf[0].rect)
                self.assertIsNotNone(blob)
                with patch('magic_converter.core.pdf.pdf_paint.check_cancelled', side_effect=[None, JobCancelled]):
                    with self.assertRaises(JobCancelled):
                        paint.render(range(len(paint.log)), pdf[0].rect)


if __name__ == '__main__': unittest.main()
