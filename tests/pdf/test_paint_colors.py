"""Retained PDF images must use the same colour space as normal rendering."""
import io
import unittest

import pymupdf as fitz
from PIL import Image

from magic_converter.core.pdf.pdf_paint import PdfPaint


class PaintColourTests(unittest.TestCase):
    def test_isolated_rgb_gray_and_cmyk_images_match_normal_renderer(self):
        # A non-null, empty FzSeparations(0) changes even ordinary RGB JPEGs
        # (21, 40, 179 -> 49, 64, 154). CMYK is the unaffected control.
        for mode, colour in [('RGB', (20, 40, 180)), ('L', 60),
                             ('CMYK', (200, 90, 10, 40))]:
            with self.subTest(mode=mode), fitz.open() as pdf:
                stream = io.BytesIO()
                Image.new(mode, (80, 60), colour).save(stream, 'JPEG')
                page = pdf.new_page(width=144, height=108)
                page.insert_image(page.rect, stream=stream.getvalue())
                paint = PdfPaint(page)
                png, bounds = paint.render(range(len(paint.log)), page.rect, dpi=72)
                reference = page.get_pixmap(dpi=72, alpha=True)
                with Image.open(io.BytesIO(png)) as actual:
                    self.assertEqual(bounds, page.rect)
                    self.assertEqual(actual.size, (reference.width, reference.height))
                    self.assertEqual(actual.tobytes(), reference.samples)

    def test_filter_keeps_selected_colours_and_transparent_margins(self):
        with fitz.open() as pdf:
            page = pdf.new_page(width=144, height=108)
            page.draw_rect(page.rect, fill=(1, 0, 0), color=None)
            stream = io.BytesIO()
            image = Image.new('RGBA', (80, 60), (20, 40, 180, 128))
            image.putpixel((0, 0), (0, 0, 0, 0))
            image.save(stream, 'PNG')
            page.insert_image((20, 20, 100, 80), stream=stream.getvalue())
            paint = PdfPaint(page)
            selected = {i for i, (kind, _) in enumerate(paint.log) if kind == 'fill-image'}
            png, bounds = paint.render(selected, page.rect, dpi=72)
            with fitz.open() as control:
                reference_page = control.new_page(width=144, height=108)
                reference_page.insert_image((20, 20, 100, 80), stream=stream.getvalue())
                expected = reference_page.get_pixmap(dpi=72, alpha=True).tobytes('png')
            with Image.open(io.BytesIO(png)) as actual, Image.open(io.BytesIO(expected)) as reference:
                self.assertEqual(bounds, fitz.Rect(20, 20, 100, 80))
                self.assertEqual(actual.tobytes(), reference.crop((20, 20, 100, 80)).tobytes())
                self.assertEqual(actual.getpixel((20, 20))[3], 128)
                self.assertEqual(actual.getpixel((0, 0))[3], 0)


if __name__ == '__main__':
    unittest.main()
