"""CJK advance rounding must not accumulate across a fixed PDF line."""
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import pymupdf as fitz
from pptx import Presentation

from magic_converter.core.pdf.pdf_fonts import resolve_font
from magic_converter.core.pdf.pdf_layout import PdfCharacter, PdfTextSpan, text_spans
from magic_converter.core.pdf.pdf_pptx_text import positioned_spans
from magic_converter.core.conversion.converter import pdf_to_pptx_editable


class CjkPositionTests(unittest.TestCase):
    def span(self, text):
        chars = tuple(PdfCharacter(c, (30 + i * 11.065, 80)) for i, c in enumerate(text))
        width = len(text) * 11.065
        return PdfTextSpan((30, 68, 30 + width, 84), text, 11.03, 0, 4, 'MalgunGothic',
                           ((30, 68), (30 + width, 68), (30, 84), (30 + width, 84)),
                           origin=(30, 80), characters=chars)

    def test_long_cjk_is_anchored_without_changing_text_or_source_positions(self):
        span = self.span('가나다라마바사아자차카타파하' * 3)
        regions = positioned_spans(span)
        self.assertGreater(len(regions), 1)
        self.assertEqual(''.join(s.text for s in regions), span.text)
        self.assertEqual(tuple(c for s in regions for c in s.characters), span.characters)
        for region in regions:
            self.assertEqual(region.origin, region.characters[0].origin)
            self.assertEqual(region.quad[0][0], region.origin[0])
            self.assertLessEqual(len(region.characters), 8)

    def test_latin_words_clusters_and_short_controls_keep_one_region(self):
        for text in ('A long ordinary English sentence stays editable', '가나다라마바사',
                     ' 제18장. 공식 참고 경로', 'Node.js 공식 웹사이트 및 LTS 다운로드',
                     '가나다라마바사아자차카타파하'*3+'\u0301', '가나다라마바사아자차카타파하'*3+'\ufe0f',
                     '가나다라마바사아자차카타파하'*3+'\u200d'):
            with self.subTest(text=text):
                span = self.span(text)
                self.assertEqual(positioned_spans(span), (span,))
        regions = positioned_spans(self.span('가나다라마바사아 VeryLongEnglishWord 자차카타파하가나다라'))
        self.assertTrue(any('VeryLongEnglishWord' in s.text for s in regions))

    def test_ambiguous_geometry_keeps_original_region(self):
        span = self.span('가나다라마바사아자차카타파하' * 3)
        self.assertEqual(positioned_spans(replace(span, direction=(-1, 0))), (replace(span, direction=(-1, 0)),))

    def test_pdf_deck_keeps_cjk_editable_and_reports_actual_region_count(self):
        face = resolve_font('Malgun Gothic')
        if face is None:
            self.skipTest('Malgun Gothic is not installed')
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory); source = root/'source.pdf'; target = root/'target.pptx'
            text = '가나다라마바사아자차카타파하' * 3
            with fitz.open() as pdf:
                page = pdf.new_page(width=560, height=150)
                page.insert_font(fontname='local', fontfile=str(face.path))
                for i, c in enumerate(text):
                    page.insert_text((30+i*11.065,80),c,fontname='local',fontsize=11.03)
                pdf.save(source)
            pdf_to_pptx_editable(source,target)
            shapes = list(Presentation(target).slides[0].shapes)
            self.assertGreater(len(shapes),1)
            self.assertTrue(all(s.has_text_frame and s.text_frame.word_wrap is False for s in shapes))
            self.assertEqual(''.join(s.text for s in shapes),text)
            self.assertTrue(all(s.name.startswith('PDF text ') for s in shapes))


if __name__ == '__main__': unittest.main()
