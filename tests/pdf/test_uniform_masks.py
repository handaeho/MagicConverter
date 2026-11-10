import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz
from pptx import Presentation
from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import type3_compound_curve_pdf


class UniformMaskTests(unittest.TestCase):
    def test_masked_multiple_glyphs_require_strictly_disjoint_complete_outline_boxes(self):
        for case in ('disjoint', 'overlapping', 'touching', 'near'):
            with self.subTest(case=case), tempfile.TemporaryDirectory() as folder:
                source = Path(folder) / 'multiple.pdf'
                type3_compound_curve_pdf(source, f'masked-{case}-glyphs')
                target = source.with_suffix('.pptx')
                pdf_to_pptx_editable(source, target)
                shapes = Presentation(target).slides[0].shapes
                groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
                if case == 'disjoint':
                    self.assertFalse(any(s.shape_type == 13 for s in shapes))
                    self.assertEqual(len(groups), 1)
                    self.assertEqual(len(groups[0].shapes[0]._element.xpath('.//a:cubicBezTo')), 16)
                    alpha = groups[0].shapes[0]._element.xpath('.//a:solidFill/a:srgbClr/a:alpha')
                    self.assertEqual([int(n.get('val')) for n in alpha], [50000])
                else:
                    self.assertEqual(sum(s.shape_type == 13 for s in shapes), 1)
                    self.assertFalse(groups)

    def convert(self,folder,variant):
        source=Path(folder)/'source.pdf';type3_compound_curve_pdf(source,'masked-gradient'if variant=='gradient'else'masked')
        if variant not in ('uniform','gradient'):
            with fitz.open(source)as pdf:
                resources=int(pdf.xref_get_key(pdf[0].xref,'Resources')[1].split()[0])
                form=int(pdf.xref_get_key(resources,'ExtGState/SM/SMask/G')[1].split()[0])
                streams={'partial':b'0.5 g 20 0 12 240 re f',
                         'multiple':b'0.5 g 0 0 360 240 re f 0.8 g 20 90 12 30 re f',
                         'tiled':b'0.5 g 0 0 30 240 re f 30 0 330 240 re f',
                         'overlap':b'0.5 g 0 0 32 240 re f 28 0 332 240 re f',
                         'gap':b'0.5 g 0 0 29.999 240 re f 30 0 330 240 re f',
                         'budget':b'0.5 g '+b'0 0 360 240 re f '*33,
                         'clipped':b'20 0 12 240 re W n 0.5 g 0 0 360 240 re f'}
                if variant in streams:pdf.update_stream(form,streams[variant])
                if variant=='transfer':
                    pdf.xref_set_key(resources,'ExtGState/SM/SMask/TR','<< /FunctionType 2 /Domain [0 1] /C0 [1] /C1 [0] /N 1 >>')
                pdf.saveIncr()
        target=source.with_suffix('.pptx');pdf_to_pptx_editable(source,target)
        return Presentation(target).slides[0].shapes

    def test_uniform_luminance_mask_keeps_original_cubics_as_editable_alpha(self):
        for variant in ('uniform','tiled','overlap'):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,variant)
                self.assertFalse(any(s.shape_type==13 for s in shapes))
                group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
                self.assertEqual(len(group.shapes[0]._element.xpath('.//a:cubicBezTo')),8)
                alpha=group.shapes[0]._element.xpath('.//a:solidFill/a:srgbClr/a:alpha')
                self.assertEqual([int(n.get('val'))for n in alpha],[50000])

    def test_variable_partial_multiple_clipped_and_transfer_masks_keep_replay(self):
        for variant in('gradient','partial','multiple','clipped','transfer','gap','budget'):
            with self.subTest(variant=variant),tempfile.TemporaryDirectory()as folder:
                shapes=self.convert(folder,variant)
                self.assertEqual(sum(s.shape_type==13 for s in shapes),1)
                self.assertFalse(any(s.name.startswith('PDF glyph outline region ')for s in shapes))
