import unittest

import pymupdf as fitz

from magic_converter.core.documents.workbook_text import write_plain_page


class WorkbookTextTests(unittest.TestCase):
    def test_batched_native_text_matches_public_writer_pixels_text_and_font_fallback(self):
        for font_name in ('cjk', 'cour'):
            with self.subTest(font=font_name), fitz.open() as reference, fitz.open() as actual:
                font = fitz.Font(font_name)
                header = 'Workbook / 1-80 / Cell values and formulas'
                lines = [f'A{row} Latin 123 한글 日本語 Ελληνικά Кириллица 😀'
                         for row in range(1, 81)]
                expected_page = reference.new_page(width=842, height=595)
                writer = fitz.TextWriter(expected_page.rect)
                writer.append((24, 30), header, font=font, fontsize=8)
                for index, value in enumerate(lines):
                    lane, offset = divmod(index, 59)
                    writer.append((24 + lane * (842 - 48) / 3, 46 + offset * 9),
                                  value, font=font, fontsize=7)
                writer.write_text(expected_page)
                actual_page = actual.new_page(width=842, height=595)
                write_plain_page(actual_page, header, lines, font, 3)
                self.assertEqual(actual_page.get_text(), expected_page.get_text())
                self.assertEqual(actual_page.get_pixmap().samples, expected_page.get_pixmap().samples)
