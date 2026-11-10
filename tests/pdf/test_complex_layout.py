"""Keep dense baselines, table structure and translucent overlap separately editable."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from pptx import Presentation
from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.diagnostics.pdf_fixtures import overlapping_glyph_pdf, tight_table_pdf


class ComplexLayoutTests(unittest.TestCase):
    def test_dense_cells_keep_fixed_text_and_normal_cells_keep_native_text(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'tight.pdf'; tight_table_pdf(source)
            warnings = []
            pdf_to_pptx_editable(source, source.with_suffix('.pptx'), warning=warnings.append)
            shapes = Presentation(source.with_suffix('.pptx')).slides[0].shapes
            tables = [s.table for s in shapes if s.has_table]
            self.assertEqual(len(tables), 1)
            table = tables[0]
            self.assertEqual((len(table.rows), len(table.columns)), (2, 2))
            self.assertEqual(table.cell(0, 0).text, '')
            self.assertEqual(table.cell(1, 0).text, '')
            self.assertIn('Normal line 1 WWW iii', table.cell(0, 1).text.replace('\xa0', ' '))
            self.assertIn('Control 1 WWW iii', table.cell(1, 1).text.replace('\xa0', ' '))
            regions = [s for s in shapes if s.name.startswith('PDF cell R')]
            self.assertEqual(len(regions), 6)
            self.assertEqual(len({s.text for s in regions}), 6)
            self.assertTrue(all(s.text_frame.word_wrap is False for s in regions))
            self.assertEqual(sum('셀 내부 문자 편집 제한' in w for w in warnings), 1)
            self.assertFalse(any(s.shape_type == 13 for s in shapes))

    def test_overlap_retains_two_alpha_paths_with_no_raster_or_text_duplicate(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'overlap.pdf'; overlapping_glyph_pdf(source)
            pdf_to_pptx_editable(source, source.with_suffix('.pptx'))
            shapes = Presentation(source.with_suffix('.pptx')).slides[0].shapes
            groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
            self.assertEqual(len(groups), 1)
            self.assertEqual(len(groups[0].shapes), 2)
            self.assertEqual([s.text for s in shapes if s.has_text_frame and s.text], ['Independent title'])
            self.assertFalse(any(s.shape_type == 13 for s in shapes))

    def test_overlap_budget_exhaustion_keeps_one_complete_replay_region(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'overlap.pdf'; overlapping_glyph_pdf(source)
            with patch('magic_converter.core.pdf.pdf_pptx_glyphs.MAX_GLYPHS', 1):
                pdf_to_pptx_editable(source, source.with_suffix('.pptx'))
            shapes = Presentation(source.with_suffix('.pptx')).slides[0].shapes
            self.assertEqual(sum(s.shape_type == 13 for s in shapes), 1)
            self.assertFalse(any(s.name.startswith('PDF glyph outline region ') for s in shapes))

    def test_overlapping_alpha_strokes_keep_separate_filled_silhouettes(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory)/'stroke.pdf'
            with fitz.open() as doc:
                page = doc.new_page(width=220, height=160)
                page.insert_text((40, 90), 'WW', fontsize=60, render_mode=1, stroke_opacity=.5)
                stream = doc.get_new_xref(); doc.update_object(stream, '<<>>')
                doc.update_stream(stream, page.read_contents().replace(b'BT', b'BT -50 Tc'))
                page.set_contents(stream); doc.save(source)
            pdf_to_pptx_editable(source, source.with_suffix('.pptx'))
            shapes = Presentation(source.with_suffix('.pptx')).slides[0].shapes
            self.assertEqual(len(shapes), 1)
            self.assertEqual(shapes[0].shape_type, 6)
            self.assertEqual(len(shapes[0].shapes), 2)
            self.assertEqual([int(a.get('val')) for s in shapes[0].shapes
                              for a in s._element.xpath('./p:spPr/a:solidFill/a:srgbClr/a:alpha')], [50000, 50000])
            self.assertTrue(all(s._element.xpath('./p:spPr/a:ln/a:noFill') for s in shapes[0].shapes))


if __name__ == '__main__':
    unittest.main()
