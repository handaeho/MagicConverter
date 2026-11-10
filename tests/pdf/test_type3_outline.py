import tempfile,unittest
from pathlib import Path
from unittest.mock import patch
import pymupdf as fitz
from pptx import Presentation
from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import clipped_sequence_pdf

ELLIPSE=(b'600 0 0 0 600 700 d1 300 0 m '
         b'465.685425 0 600 156.700675 600 350 c '
         b'600 543.299325 465.685425 700 300 700 c '
         b'134.314575 700 0 543.299325 0 350 c '
         b'0 156.700675 134.314575 0 300 0 c h f')

class Type3OutlineTests(unittest.TestCase):
    def convert(self, folder, *, clip='0 0 360 240', mask=False, program=None, text_red=False):
        source=Path(folder)/'source.pdf';clipped_sequence_pdf(source,clip,type3=True,mask=mask)
        if program is not None or text_red:
            edited=source.with_name('edited.pdf')
            with fitz.open(source)as pdf:
                if program is not None:
                    for xref in range(1,pdf.xref_length()):
                        if pdf.xref_is_stream(xref) and b'600 0 0 0 600 700 d1' in pdf.xref_stream(xref):
                            pdf.update_stream(xref,program)
                if text_red:
                    stream=pdf[0].get_contents()[0]
                    pdf.update_stream(stream,pdf.xref_stream(stream).replace(b'/T3 40 Tf',b'1 0 0 rg /T3 40 Tf'))
                pdf.save(edited)
            source=edited
        target=source.with_suffix('.pptx');pdf_to_pptx_editable(source,target)
        shapes=Presentation(target).slides[0].shapes
        self.assertIn('Independent title',[s.text for s in shapes if s.has_text_frame])
        return shapes

    def test_single_black_path_is_an_editable_curve(self):
        with tempfile.TemporaryDirectory()as folder:
            shapes=self.convert(folder)
            self.assertEqual(sum(s.shape_type==13 for s in shapes),0)
            group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
            self.assertEqual(len(group.shapes),1)
            self.assertTrue(group.shapes[0]._element.xpath('./p:spPr/a:custGeom'))

    def test_convex_bezier_contour_retains_original_cubic_commands(self):
        with tempfile.TemporaryDirectory()as folder:
            shapes=self.convert(folder,program=ELLIPSE)
            self.assertFalse(any(s.shape_type==13 for s in shapes))
            group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
            self.assertEqual(len(group.shapes),1)
            self.assertEqual(len(group.shapes[0]._element.xpath('.//a:cubicBezTo')),4)

    def test_closed_two_cubic_lens_and_single_cubic_do_not_need_a_chord_polygon(self):
        programs=[(b'600 0 0 0 600 700 d1 300 0 m 600 0 600 700 300 700 c 0 700 0 0 300 0 c h f',2),
                  (b'600 0 0 0 600 700 d1 300 0 m 600 700 0 700 300 0 c h f',1)]
        for program,count in programs:
            with self.subTest(cubics=count),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,program=program)
                self.assertFalse(any(s.shape_type==13 for s in shapes))
                group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
                self.assertEqual(len(group.shapes[0]._element.xpath('.//a:cubicBezTo')),count)
        # A lens needs refined bounds to certify its nested rectangular hole.
        nested=(b'600 0 0 0 600 700 d1 300 0 m 600 0 600 700 300 700 c 0 700 0 0 300 0 c h '
                b'260 300 m 260 400 l 340 400 l 340 300 l h f')
        with tempfile.TemporaryDirectory()as folder:
            self.assertEqual(sum(s.shape_type==13 for s in self.convert(folder,program=nested)),0)

    def test_retraced_nonzero_contours_are_not_mistaken_for_simple_convex_ink(self):
        programs=[b'600 0 0 0 600 700 d1 0 0 m 600 0 l 300 700 l 0 0 l 600 0 l 300 700 l h f',
                  ELLIPSE.replace(b'h f',ELLIPSE.split(b'300 0 m ',1)[1])]
        for program in programs:
            with self.subTest(program=program),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,program=program)
                self.assertEqual(sum(s.shape_type==13 for s in shapes),1)

    def test_unsafe_curve_control_polygon_retains_complete_replay(self):
        for program in [b'600 0 0 0 600 700 d1 0 0 m 900 900 -300 900 600 0 c 300 700 l h f',
                        ELLIPSE.replace(b'465.685425 0',b'-300 700',1)]:
            with self.subTest(program=program),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,program=program)
                self.assertEqual(sum(s.shape_type==13 for s in shapes),1)

    def test_curve_mask_clip_and_colour_keep_the_whole_original_paint(self):
        for options in ({'mask':'gradient'},{'clip':'20 110 12 30'},{'text_red':True}):
            with self.subTest(options=options),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,program=ELLIPSE,**options)
                self.assertEqual(sum(s.shape_type==13 for s in shapes),1)
                self.assertFalse(any(s.name.startswith('PDF glyph outline region ')for s in shapes))

    def test_mask_colour_and_multiple_paints_keep_complete_replay(self):
        programs=[{'mask':'gradient'},{'text_red':True},
                  {'program':b'600 0 d0 1 0 0 rg 0 0 m 600 0 l 300 700 l h f'},
                  {'program':b'600 0 0 0 600 700 d1 0 0 100 100 re f 200 200 100 100 re f'},
                  {'program':b'600 0 0 0 600 700 d1 0 0 m 300 700 l 600 0 l 0 450 l 600 450 l h f'}]
        for options in programs:
            with self.subTest(options=options),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,**options)
                self.assertEqual(sum(s.shape_type==13 for s in shapes),1)
                self.assertFalse(any(s.name.startswith('PDF glyph outline region ')for s in shapes))

    def test_exhausted_geometry_budget_cannot_emit_a_partial_curve(self):
        with tempfile.TemporaryDirectory()as folder,patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_COMMANDS',1):
            shapes=self.convert(folder)
            self.assertEqual(sum(s.shape_type==13 for s in shapes),1)

if __name__=='__main__':unittest.main()
