"""Stored PDF cell baselines must survive editable DrawingML paragraphs."""
from dataclasses import replace
import math
import unittest

from magic_converter.core.pdf.pdf_layout import PdfCell, PdfTextSpan
from magic_converter.core.pdf.pdf_pptx_text import cell_vertical_layout


class CellBaselineTests(unittest.TestCase):
    def line(self, y, size=12):
        return [PdfTextSpan((10, y-size, 90, y+3), 'Text', size, 0, 0, 'Arial',
                            ((10,y-size),(90,y-size),(10,y+3),(90,y+3)), origin=(10,y))]

    def test_loose_lines_preserve_baselines_and_variable_font_sizes(self):
        cell=PdfCell(0,0,1,1,(0,0,100,120))
        lines=[self.line(30),self.line(65,16),self.line(100,10)]
        top,leading,after=cell_vertical_layout(cell,lines)
        baseline=top+leading[0]*.75
        self.assertAlmostEqual(baseline,30)
        for i,expected in enumerate((65,100)):
            baseline+=leading[i]*.25+after[i]+leading[i+1]*.75
            self.assertAlmostEqual(baseline,expected)

    def test_low_single_line_is_not_clamped_to_cell_height_third(self):
        cell=PdfCell(0,0,1,1,(0,0,100,120))
        top,_,_=cell_vertical_layout(cell,[self.line(100)])
        self.assertGreater(top,40)

    def test_compact_cell_uses_font_box_without_negative_paragraph_gaps(self):
        cell=PdfCell(0,0,1,1,(0,0,100,90))
        lines=[self.line(y) for y in (15,33,51,69)]
        top,leading,after=cell_vertical_layout(cell,lines)
        self.assertTrue(all(v>=0 for v in after))
        baseline=top+.75*leading[0]
        self.assertAlmostEqual(baseline,15)
        for i,expected in enumerate((33,51,69)):
            baseline+=.25*leading[i]+after[i]+.75*leading[i+1]
            self.assertAlmostEqual(baseline,expected)

    def test_compact_cell_rejects_tall_or_unknown_font_metrics(self):
        cell=PdfCell(0,0,1,1,(0,0,100,90))
        for ascender in (1.6,math.nan):
            lines=[[replace(self.line(y)[0],ascender=ascender)] for y in (15,33,51)]
            self.assertIsNone(cell_vertical_layout(cell,lines))

    def test_dense_or_outside_lines_keep_existing_layout(self):
        cell=PdfCell(0,0,1,1,(0,0,100,120))
        for lines in ([self.line(30),self.line(43)], [self.line(10)],
                      [self.line(119)], [], [self.line(50),self.line(30)]):
            with self.subTest(lines=lines):
                self.assertIsNone(cell_vertical_layout(cell,lines))

    def test_rotated_vertical_and_nonfinite_lines_are_not_repositioned(self):
        cell=PdfCell(0,0,1,1,(0,0,100,120))
        for change in ({'direction':(0,1)}, {'writing_mode':1}, {'origin':(10,math.nan)}):
            self.assertIsNone(cell_vertical_layout(cell,[[replace(self.line(30)[0],**change)]]))


if __name__=='__main__':unittest.main()
