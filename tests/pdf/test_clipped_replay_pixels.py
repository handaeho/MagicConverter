import io
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageChops, ImageStat

from magic_converter.core.diagnostics.pdf_fixtures import clipped_sequence_pdf
from magic_converter.core.pdf.pdf_paint import PdfPaint


class ClippedReplayPixelTests(unittest.TestCase):
    def test_partial_clips_preserve_local_pixels_and_cannot_regrow_ink(self):
        for type3 in (False, True):
            for clip in ('20 110 12 30', '22 125 8 12', '21.3 122.7 9.4 11.2'):
                with self.subTest(type3=type3,clip=clip),tempfile.TemporaryDirectory()as folder:
                    path=Path(folder)/'control.pdf'
                    clipped_sequence_pdf(path,clip,type3=type3)
                    with fitz.open(path)as pdf:
                        page=pdf[0];paint=PdfPaint(page);sequence=len(paint.log)-1
                        for dpi in (72,96,144,200,300):
                            rendered=paint.render([sequence],paint.visible_bounds(sequence),dpi=dpi)
                            self.assertIsNotNone(rendered)
                            data,placed=rendered;pixel=72/dpi
                            self.assertTrue((paint.states[sequence].clip+(-pixel,-pixel,pixel,pixel)).contains(placed))
                            pix=page.get_pixmap(dpi=dpi,alpha=False)
                            with Image.frombytes('RGB',(pix.width,pix.height),pix.samples)as reference:
                                with Image.new('RGB',reference.size,'white')as canvas:
                                    with Image.open(io.BytesIO(data))as ink:
                                        canvas.paste(ink,(round(placed.x0*dpi/72),round(placed.y0*dpi/72)),ink)
                                    crop=tuple((fitz.Rect(15,80,70,145)*(dpi/72)).irect)
                                    with reference.crop(crop)as a,canvas.crop(crop)as b:
                                        with ImageChops.difference(a,b)as difference:
                                            self.assertLess(sum(ImageStat.Stat(difference).mean)/3,.1)
