import tempfile
import io
import unittest
from pathlib import Path
from unittest.mock import patch
import pymupdf
from pptx import Presentation
from PIL import Image
from magic_converter.core.pdf.pdf_paint import PaintSequenceMismatch
from magic_converter.core.conversion.converter import pdf_to_pptx_editable


class PaintFallbackTests(unittest.TestCase):
    def test_mismatched_sequence_preserves_whole_page_with_explicit_notice(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder)/'input.pdf'; output = source.with_suffix('.pptx')
            with pymupdf.open() as doc:
                page = doc.new_page(width=240,height=160); page.insert_text((20,40),'Original visible page')
                expected = page.get_pixmap(dpi=200,alpha=False).tobytes('png'); doc.save(source)
            notices=[]
            with patch('magic_converter.core.pdf.pdf_pptx_objects.PdfPaint',side_effect=PaintSequenceMismatch('test')):
                pdf_to_pptx_editable(source,output,warning=notices.append)
            slide=Presentation(output).slides[0]
            self.assertEqual(len(slide.shapes),1)
            with Image.open(io.BytesIO(slide.shapes[0].image.blob)) as actual, Image.open(io.BytesIO(expected)) as reference:
                self.assertEqual(actual.size, reference.size)
                self.assertEqual(actual.tobytes(), reference.tobytes())
            self.assertTrue(any('편집 제한' in n for n in notices))
            with patch('magic_converter.core.pdf.pdf_pptx_objects.PdfPaint',side_effect=ValueError('other error')):
                with self.assertRaisesRegex(ValueError,'other error'): pdf_to_pptx_editable(source,output)


if __name__ == '__main__': unittest.main()
