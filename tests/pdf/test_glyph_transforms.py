"""Character origins alone cannot reproduce stretched/mirrored glyph ink."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from pptx import Presentation

from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import glyph_scale_pdf
from magic_converter.core.engines.office_engine import find_office
from magic_converter.core.pdf.pdf_paint import PdfPaint


class GlyphTransformTests(unittest.TestCase):
    def test_ordinary_alpha_text_remains_character_editable(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'ordinary-alpha.pdf'
            with fitz.open() as pdf:
                page=pdf.new_page(width=400,height=200)
                page.insert_text((30,80),'Normal alpha text',fontsize=20,fill_opacity=.5)
                pdf.save(source)
            pdf_to_pptx_editable(source,source.with_suffix('.pptx'))
            shapes=Presentation(source.with_suffix('.pptx')).slides[0].shapes
            texts=[s for s in shapes if s.has_text_frame and s.text]
            self.assertEqual([s.text for s in texts],['Normal alpha text'])
            self.assertTrue(all(49800<int(a.get('val'))<50300 for s in texts for a in s._element.xpath('.//a:alpha')))
            self.assertTrue(texts[0]._element.xpath('.//a:alpha'))
            self.assertFalse(any(s.name.startswith('PDF glyph outline region ') for s in shapes))

    def test_stroke_and_fill_stroke_keep_paint_sequence_and_independent_text(self):
        for mode in (1,2):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as directory:
                source=Path(directory)/'outlined.pdf'
                with fitz.open() as pdf:
                    page=pdf.new_page(width=400,height=240)
                    page.insert_text((30,60),'Independent before',fontsize=20)
                    page.insert_text((30,125),'Outline glyphs',fontsize=20,render_mode=mode)
                    page.insert_text((30,190),'Independent after',fontsize=20)
                    paint=PdfPaint(page)
                    self.assertEqual(len(paint.states),len(paint.log))
                    pdf.save(source)
                deck=source.with_suffix('.pptx');pdf_to_pptx_editable(source,deck)
                shapes=Presentation(deck).slides[0].shapes
                self.assertEqual([s.text for s in shapes if s.has_text_frame],['Independent before','Independent after'])
                groups=[s for s in shapes if s.name.startswith('PDF glyph outline region ')]
                self.assertEqual(len(groups),1)
                self.assertEqual(len(groups[0].shapes),mode)
                self.assertEqual(sum(s.shape_type==13 for s in shapes),0)

    def test_normal_text_stays_editable_and_nonuniform_glyphs_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.pdf';glyph_scale_pdf(source)
            with fitz.open(source) as pdf:
                self.assertEqual([s.native_text for s in PdfPaint(pdf[0]).states], [True, False, False])
            deck=source.with_suffix('.pptx');pdf_to_pptx_editable(source,deck)
            shapes=Presentation(deck).slides[0].shapes
            self.assertEqual([s.text for s in shapes if s.has_text_frame], ['Glyph widths WWW iii'])
            self.assertEqual(sum(s.name.startswith('PDF glyph outline region ') for s in shapes),2)
            self.assertEqual(sum(s.shape_type==13 for s in shapes),0)

    def test_outline_budget_failure_keeps_complete_raster_regions(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.pdf';glyph_scale_pdf(source)
            with patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_GLYPHS',1):
                pdf_to_pptx_editable(source,source.with_suffix('.pptx'))
            shapes=Presentation(source.with_suffix('.pptx')).slides[0].shapes
            self.assertEqual([s.text for s in shapes if s.has_text_frame],['Glyph widths WWW iii'])
            self.assertEqual(sum(s.shape_type==13 for s in shapes),2)
            self.assertFalse(any(s.name.startswith('PDF glyph outline region ') for s in shapes))

    def test_command_budget_or_missing_outline_falls_back_atomically(self):
        for target,value in [('MAX_COMMANDS',1),('missing',None)]:
            with self.subTest(target=target),tempfile.TemporaryDirectory() as directory:
                source=Path(directory)/'source.pdf';glyph_scale_pdf(source)
                override=(patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_COMMANDS',value)
                          if target=='MAX_COMMANDS' else patch('magic_converter.core.pdf.pdf_pptx_glyphs.m.fz_outline_glyph',side_effect=RuntimeError('No outline')))
                with override:pdf_to_pptx_editable(source,source.with_suffix('.pptx'))
                shapes=Presentation(source.with_suffix('.pptx')).slides[0].shapes
                self.assertEqual(sum(s.shape_type==13 for s in shapes),2)
                self.assertFalse(any(s.name.startswith('PDF glyph outline region ') for s in shapes))

    def test_rotation_and_uniform_scale_pass_but_skew_and_reflection_do_not(self):
        cases=[(fitz.Matrix(30),True),(fitz.Matrix(1.5,1.5),True),
               (fitz.Matrix(1,0,.4,1,0,0),False),(fitz.Matrix(-1,1),False)]
        for matrix,expected in cases:
            with self.subTest(matrix=matrix),fitz.open() as pdf:
                page=pdf.new_page(width=400,height=400)
                point=fitz.Point(200,200)
                page.insert_text(point,'Glyph',fontsize=20,morph=(point,matrix))
                self.assertEqual(PdfPaint(page).states[0].native_text,expected)

    def test_partial_clip_keeps_replay_and_translucent_overlap_keeps_separate_curves(self):
        for clipped in (False,True):
            with self.subTest(clipped=clipped),tempfile.TemporaryDirectory() as directory:
                source=Path(directory)/'source.pdf'
                with fitz.open() as pdf:
                    page=pdf.new_page(width=200,height=120);point=fitz.Point(30,70)
                    page.insert_text(point,'WW',fontsize=20,morph=(point,fitz.Matrix(.7,1)),
                                     fill_opacity=1 if clipped else .5)
                    content=page.read_contents()
                    if clipped:
                        content=b'q 40 0 130 120 re W n\n'+content+b'\nQ'
                    else:
                        content=content.replace(b'BT',b'BT -18 Tc')
                    stream=pdf.get_new_xref();pdf.update_object(stream,'<<>>')
                    pdf.update_stream(stream,content);page.set_contents(stream)
                    pdf.save(source)
                pdf_to_pptx_editable(source,source.with_suffix('.pptx'))
                shapes=Presentation(source.with_suffix('.pptx')).slides[0].shapes
                self.assertEqual(len(shapes),1)
                if clipped:
                    self.assertEqual(shapes[0].shape_type,13)
                    self.assertGreaterEqual(shapes[0].left.pt,39.9)
                else:
                    self.assertEqual(shapes[0].shape_type,6)
                    self.assertEqual(len(shapes[0].shapes),2)
                    self.assertEqual([int(a.get('val')) for s in shapes[0].shapes
                                      for a in s._element.xpath('.//a:alpha')],[50000,50000])

    def test_nonoverlapping_translucent_ink_keeps_alpha_but_complex_group_keeps_replay(self):
        from magic_converter.core.diagnostics.pdf_fixtures import glyph_alpha_pdf
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'alpha.pdf';glyph_alpha_pdf(source)
            pdf_to_pptx_editable(source,source.with_suffix('.pptx'))
            shapes=Presentation(source.with_suffix('.pptx')).slides[0].shapes
            groups=[s for s in shapes if s.name.startswith('PDF glyph outline region ')]
            self.assertEqual(len(groups),2)
            self.assertEqual([len(g.shapes) for g in groups],[1,1])
            self.assertEqual([int(a.get('val')) for g in groups for s in g.shapes
                              for a in s._element.xpath('.//a:alpha')],[25000,50000])
            self.assertEqual(sum(s.shape_type==13 for s in shapes),1)
            self.assertEqual([s.text for s in shapes if s.has_text_frame and s.text],['Independent title'])

    def test_real_simple_type3_glyph_without_outline_is_an_editable_curve(self):
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.pdf'
            with fitz.open() as pdf:
                page=pdf.new_page(width=200,height=180)
                page.insert_text((20,30),'Independent',fontsize=12)
                proc=pdf.get_new_xref();pdf.update_object(proc,'<<>>')
                pdf.update_stream(proc,b'600 0 0 0 600 700 d1 0 0 m 600 0 l 300 700 l h f')
                font=pdf.get_new_xref()
                pdf.update_object(font,'<< /Type /Font /Subtype /Type3 /Name /T3 /FontBBox [0 0 600 700] '
                    '/FontMatrix [.001 0 0 .001 0 0] /FirstChar 65 /LastChar 65 /Widths [600] '
                    f'/Encoding << /Type /Encoding /Differences [65 /A] >> /CharProcs << /A {proc} 0 R >> /Resources <<>> >>')
                resources=int(pdf.xref_get_key(page.xref,'Resources')[1].split()[0])
                pdf.xref_set_key(resources,'Font/T3',f'{font} 0 R')
                content=page.read_contents()+b' q BT /T3 30 Tf 1 0 0 1 40 60 Tm (A) Tj ET Q'
                stream=pdf.get_new_xref();pdf.update_object(stream,'<<>>');pdf.update_stream(stream,content)
                page.set_contents(stream);pdf.save(source)
            pdf_to_pptx_editable(source,source.with_suffix('.pptx'))
            shapes=Presentation(source.with_suffix('.pptx')).slides[0].shapes
            self.assertEqual([s.text for s in shapes if s.has_text_frame],['Independent'])
            self.assertEqual(sum(s.shape_type==13 for s in shapes),0)
            groups=[s for s in shapes if s.name.startswith('PDF glyph outline region ')]
            self.assertEqual(len(groups),1)
            self.assertEqual(len(groups[0].shapes),1)
            self.assertEqual(len(groups[0].shapes[0]._element.xpath('.//a:custGeom')),1)

    @unittest.skipUnless(find_office('PowerPoint'),'Desktop PowerPoint is not installed')
    def test_native_pixels_and_deleting_preserved_glyphs_leave_no_ghost(self):
        from magic_converter.core.engines.hwp_converter import run_com
        from magic_converter.core.diagnostics.visual_comparison import compare
        with tempfile.TemporaryDirectory() as directory:
            source=Path(directory)/'source.pdf';glyph_scale_pdf(source)
            deck=source.with_suffix('.pptx');pdf_to_pptx_editable(source,deck)
            native=source.with_name('native.pdf');run_com(deck,native,'office_pdf')
            self.assertLess(compare(source,native)['pages'][0]['mae'],2.5)
            presentation=Presentation(deck)
            for slide in presentation.slides:
                for shape in list(slide.shapes):
                    if shape.shape_type==13 or shape.name.startswith('PDF glyph outline region '):
                        shape._element.getparent().remove(shape._element)
            presentation.save(deck);run_com(deck,native,'office_pdf')
            with fitz.open(native) as pdf:
                self.assertIn('Glyph widths',pdf[0].get_text())
                pix=pdf[0].get_pixmap(clip=fitz.Rect(0,80,400,240))
                self.assertGreaterEqual(min(pix.samples),250)  # Office's white-page edge can antialias to 254.
                self.assertIn('Independent before',pdf[1].get_text())
                self.assertIn('Independent after',pdf[1].get_text())
                self.assertGreaterEqual(min(pdf[1].get_pixmap(clip=fitz.Rect(0,80,400,155)).samples),250)


if __name__=='__main__':unittest.main()
