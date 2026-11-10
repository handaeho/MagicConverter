"""Per-edge PDF rule styles must not collapse to the first table stroke."""
import tempfile
import unittest
from pathlib import Path
import pymupdf as fitz
from pptx import Presentation
from magic_converter.core.pdf.pdf_layout import extract_tables, text_spans
from magic_converter.core.conversion.converter import pdf_to_pptx_editable


def fixture(path, *, width=None, dashed=False):
    with fitz.open() as pdf:
        page = pdf.new_page(width=300, height=240)
        for i, x in enumerate((30, 150, 270)):
            page.draw_line((x, 30), (x, 210), width=width or (.1, .7, 3)[i],
                           color=((1, 0, 0) if i == 2 else (0, 0, 0)), dashes='[3 2] 0' if dashed else None)
        for i, y in enumerate((30, 120, 210)):
            page.draw_line((30, y), (270, y), width=width or (.1, .7, 3)[i])
        for x in (42, 162):
            for y in (65, 155):
                page.insert_text((x, y), 'Cell text', fontsize=10)
        pdf.save(path)


class TableRuleStylesTests(unittest.TestCase):
    def test_per_edge_color_and_unclamped_width_survive(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'rules.pdf'; fixture(source)
            with fitz.open(source) as pdf:
                tables = extract_tables(pdf[0], text_spans(pdf[0]))
            self.assertEqual(len(tables), 1)
            cells = {(c.row, c.col): c for c in tables[0].cells}
            self.assertAlmostEqual(cells[0, 0].borders['left'][1], .1)
            self.assertAlmostEqual(cells[0, 0].borders['right'][1], .7)
            self.assertEqual(cells[1, 1].borders['right'], ('FF0000', 3))
            target = source.with_suffix('.pptx'); pdf_to_pptx_editable(source, target)
            table = next(s.table for s in Presentation(target).slides[0].shapes if s.has_table)
            line = table.cell(0, 0)._tc.xpath('./a:tcPr/a:lnL')[0]
            self.assertAlmostEqual(int(line.get('w')) / 12700, .1, places=4)

    def test_uniform_normal_control_and_dashed_negative(self):
        for dashed, expected in ((False, 1), (True, 0)):
            with self.subTest(dashed=dashed), tempfile.TemporaryDirectory() as folder:
                source=Path(folder)/'rules.pdf'; fixture(source, width=.5, dashed=dashed)
                with fitz.open(source) as pdf:
                    self.assertEqual(len(extract_tables(pdf[0], text_spans(pdf[0]))), expected)


if __name__ == '__main__':
    unittest.main()
