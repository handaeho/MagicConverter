"""PDF-only regression cases for object editability, clipping and paint order."""

import io
import tempfile
import unittest
from pathlib import Path

import pymupdf as fitz
from PIL import Image, ImageChops, ImageStat
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE_TYPE, MSO_AUTO_SHAPE_TYPE

from magic_converter.core.conversion.converter import pdf_to_pptx_editable
from magic_converter.core.engines.office_engine import find_office
from magic_converter.core.pdf.pdf_paint import PdfPaint


class PdfObjectTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf, self.pptx = self.root / "input.pdf", self.root / "objects.pptx"

    def tearDown(self):
        self.temporary.cleanup()

    def diagram(self):
        document = fitz.open()
        page = document.new_page(width=360, height=240)
        page.draw_rect(page.rect, color=None, fill=(1, 1, 1))
        page.draw_rect((20, 30, 130, 80), color=(.1, .2, .4), fill=(.1, .4, .7))
        page.draw_circle((185, 55), 25, color=(.2, .4, .6), fill=(.5, .8, .9))
        page.draw_line((25, 150), (115, 175), color=(.5, .1, .2), width=2)
        page.draw_bezier((130, 160), (160, 100), (210, 220), (235, 160), color=(.2, .4, .5), width=2)
        image = io.BytesIO()
        Image.new("RGB", (40, 30), (245, 140, 25)).save(image, "PNG")
        page.insert_image((260, 30, 300, 60), stream=image.getvalue())
        page.insert_text((28, 60), "Editable box", fontsize=12, color=(1, 1, 1))
        # An opaque overlay must remain above text painted earlier.
        page.draw_rect((105, 45, 125, 65), color=None, fill=(1, .6, .1))
        document.save(self.pdf)
        document.close()

    def test_shapes_images_and_text_are_independent_editable_objects(self):
        self.diagram()
        pdf_to_pptx_editable(self.pdf, self.pptx)
        deck = Presentation(self.pptx)
        slide = deck.slides[0]
        self.assertEqual(sum(s.shape_type == MSO_SHAPE_TYPE.PICTURE for s in slide.shapes), 1)
        self.assertEqual(sum(s.shape_type == MSO_SHAPE_TYPE.TEXT_BOX for s in slide.shapes), 1)
        self.assertTrue(any(s.shape_type == MSO_SHAPE_TYPE.LINE for s in slide.shapes))
        self.assertTrue(any(s.shape_type == MSO_SHAPE_TYPE.FREEFORM for s in slide.shapes))
        self.assertTrue(any(s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE and s.auto_shape_type == MSO_AUTO_SHAPE_TYPE.OVAL for s in slide.shapes))
        text_index = next(i for i, s in enumerate(slide.shapes) if s.shape_type == MSO_SHAPE_TYPE.TEXT_BOX)
        self.assertEqual(slide.shapes[-1].shape_type, MSO_SHAPE_TYPE.AUTO_SHAPE)
        self.assertGreater(len(slide.shapes) - 1, text_index)
        rectangle = next(s for s in slide.shapes if s.shape_type == 1 and abs(s.left.pt - 20) < .1)
        rectangle.fill.fore_color.rgb = RGBColor(255, 0, 0)
        rectangle.left += 127000
        deck.save(self.pptx)
        edited = next(s for s in Presentation(self.pptx).slides[0].shapes if s.shape_type == 1 and abs(s.left.pt - 30) < .1)
        self.assertEqual(str(edited.fill.fore_color.rgb), "FF0000")

    def test_clipped_paths_keep_transparent_edges_and_original_pdf_pixels(self):
        document = fitz.open()
        page = document.new_page(width=240, height=180)
        stream = document.get_new_xref()
        document.update_object(stream, "<<>>")
        document.update_stream(stream, b"q 60 20 100 100 re W n 60 70 m 60 97.6 82.4 120 110 120 c 137.6 120 160 97.6 160 70 c 160 42.4 137.6 20 110 20 c 82.4 20 60 42.4 60 70 c W n 0.2 0.7 0.5 rg 60 20 100 100 re f Q")
        page.set_contents(stream)
        document.save(self.pdf)
        document.close()
        with fitz.open(self.pdf) as pdf:
            paint = PdfPaint(pdf[0])
            data, bounds = paint.render(range(len(paint.log)), pdf[0].rect, dpi=144)
            original = Image.open(io.BytesIO(pdf[0].get_pixmap(dpi=144, alpha=True).tobytes("png")))
            ink = Image.open(io.BytesIO(data))
            self.assertEqual(ink.getpixel((0, 0))[3], 0)
            canvas = Image.new("RGBA", original.size)
            canvas.alpha_composite(ink, (round(bounds.x0 * 2), round(bounds.y0 * 2)))
            # Isolated colour conversion can differ by a few 8-bit colour
            # levels; the clipping coverage itself must remain identical.
            self.assertIsNone(ImageChops.difference(original.getchannel("A"), canvas.getchannel("A")).getbbox())
            self.assertLess(max(ImageStat.Stat(ImageChops.difference(original, canvas)).mean), .5)
        pdf_to_pptx_editable(self.pdf, self.pptx)
        shapes = Presentation(self.pptx).slides[0].shapes
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0].shape_type, MSO_SHAPE_TYPE.PICTURE)
        self.assertLess(shapes[0].width.pt, 105)

    def test_group_opacity_is_not_applied_to_each_overlapping_child(self):
        document = fitz.open()
        page = document.new_page(width=240, height=180)
        form = document.get_new_xref()
        document.update_object(form, "<< /Type /XObject /Subtype /Form /BBox [0 0 240 180] /Group << /S /Transparency /I true >> /Resources <<>> >>")
        document.update_stream(form, b"1 0 0 rg 20 30 90 80 re f 0 0 1 rg 70 30 90 80 re f")
        document.xref_set_key(page.xref, "Resources", f"<< /XObject << /Fm {form} 0 R >> /ExtGState << /GS << /ca 0.5 /CA 0.5 >> >> >>")
        stream = document.get_new_xref()
        document.update_object(stream, "<<>>")
        document.update_stream(stream, b"q /GS gs /Fm Do Q")
        page.set_contents(stream)
        document.save(self.pdf)
        document.close()
        with fitz.open(self.pdf) as pdf:
            paint = PdfPaint(pdf[0])
            self.assertTrue(paint.groups)
        pdf_to_pptx_editable(self.pdf, self.pptx)
        shapes = Presentation(self.pptx).slides[0].shapes
        self.assertEqual(len(shapes), 1)
        image = Image.open(io.BytesIO(shapes[0].image.blob))
        alphas = image.getchannel("A").getextrema()
        self.assertLessEqual(alphas[1], 129)

    def test_annotations_and_rotated_cropboxes_preserve_paint_sequence(self):
        self.diagram()
        with fitz.open(self.pdf) as doc:
            page = doc[0]
            annotation = page.add_rect_annot((30, 90, 140, 130))
            annotation.set_colors(stroke=(1, 0, 0))
            annotation.update()
            page.set_cropbox((10, 10, 340, 230))
            page.set_rotation(90)
            doc.save(self.root / "annotated.pdf")
        pdf_to_pptx_editable(self.root / "annotated.pdf", self.pptx)
        deck = Presentation(self.pptx)
        self.assertEqual((deck.slide_width.pt, deck.slide_height.pt), (220, 330))
        self.assertGreater(len(deck.slides[0].shapes), 5)

    def test_soft_mask_is_preserved_when_paint_is_isolated(self):
        document = fitz.open()
        page = document.new_page(width=240, height=180)
        mask = document.get_new_xref()
        document.update_object(mask, "<< /Type /XObject /Subtype /Form /BBox [0 0 240 180] /Group << /S /Transparency /CS /DeviceGray >> /Resources <<>> >>")
        document.update_stream(mask, b"0.5 g 20 30 100 80 re f")
        document.xref_set_key(page.xref, "Resources", f"<< /ExtGState << /SM << /SMask << /S /Luminosity /G {mask} 0 R >> >> >> >>")
        stream = document.get_new_xref()
        document.update_object(stream, "<<>>")
        document.update_stream(stream, b"q /SM gs 1 0 0 rg 0 0 240 180 re f Q")
        page.set_contents(stream)
        document.save(self.pdf)
        document.close()
        pdf_to_pptx_editable(self.pdf, self.pptx)
        shapes = Presentation(self.pptx).slides[0].shapes
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0].shape_type, 13)
        self.assertLess(shapes[0].width.pt, 105)
        image = Image.open(io.BytesIO(shapes[0].image.blob))
        self.assertLess(image.getchannel("A").getextrema()[1], 250)

    def test_pattern_tiles_are_preserved_over_the_entire_fill_area(self):
        document = fitz.open()
        page = document.new_page(width=240, height=180)
        pattern = document.get_new_xref()
        document.update_object(pattern, "<< /Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources <<>> >>")
        document.update_stream(pattern, b"1 0 0 rg 0 0 5 5 re f")
        document.xref_set_key(page.xref, "Resources", f"<< /Pattern << /P {pattern} 0 R >> >>")
        stream = document.get_new_xref()
        document.update_object(stream, "<<>>")
        document.update_stream(stream, b"/Pattern cs /P scn 20 30 160 100 re f")
        page.set_contents(stream)
        document.save(self.pdf)
        document.close()
        pdf_to_pptx_editable(self.pdf, self.pptx)
        shapes = Presentation(self.pptx).slides[0].shapes
        self.assertEqual(len(shapes), 1)
        self.assertEqual(shapes[0].shape_type, 13)
        self.assertGreater(shapes[0].width.pt, 150)
        self.assertGreater(shapes[0].height.pt, 90)

    def test_implicit_rectangular_clips_and_overprinted_bold_text(self):
        document = fitz.open()
        page = document.new_page(width=240, height=180)
        page.insert_text((30, 70), "Overprinted title", fontsize=14)
        page.insert_text((29.6, 70), "Overprinted title", fontsize=14)
        page.insert_text((30, 110), "Editable body", fontsize=11)
        content = page.read_contents()
        stream = document.get_new_xref()
        document.update_object(stream, "<<>>")
        document.update_stream(stream, b"q 20 20 m 220 20 l 220 160 l 20 160 l W n\n" + content + b"\nQ")
        page.set_contents(stream)
        document.save(self.pdf)
        document.close()
        pdf_to_pptx_editable(self.pdf, self.pptx)
        shapes = Presentation(self.pptx).slides[0].shapes
        self.assertTrue(any(s.has_text_frame and s.text == "Editable body" for s in shapes))
        self.assertFalse(any(s.has_text_frame and "Overprinted" in s.text for s in shapes),
                         "Replacing one overprint must not leave a duplicate text ghost")
        groups=[s for s in shapes if s.name.startswith('PDF glyph outline region ')]
        self.assertEqual(len(groups),1)
        self.assertEqual(len(groups[0].shapes),2)

    def test_grid_with_vector_cell_icon_does_not_discard_the_icon(self):
        document = fitz.open()
        page = document.new_page(width=240, height=180)
        for x in (20, 100, 180):
            page.draw_line((x, 30), (x, 150))
        for y in (30, 90, 150):
            page.draw_line((20, y), (180, y))
        page.insert_text((30, 60), "Label", fontsize=11)
        page.insert_text((30, 120), "Value", fontsize=11)
        page.draw_circle((140, 60), 12, color=None, fill=(0, .7, .2))
        document.save(self.pdf)
        document.close()
        pdf_to_pptx_editable(self.pdf, self.pptx)
        shapes = Presentation(self.pptx).slides[0].shapes
        self.assertFalse(any(s.has_table for s in shapes))
        self.assertTrue(any(s.shape_type == 1 and s.auto_shape_type == MSO_AUTO_SHAPE_TYPE.OVAL for s in shapes))

    @unittest.skipUnless(find_office("PowerPoint"), "Desktop PowerPoint is not installed")
    def test_powerpoint_rendering_and_deletion_leave_no_raster_ghosts(self):
        from magic_converter.core.engines.hwp_converter import convert_office_pdf
        self.diagram()
        pdf_to_pptx_editable(self.pdf, self.pptx)
        rendered = self.root / "rendered.pdf"
        convert_office_pdf(self.pptx, rendered, lambda _: None)
        with fitz.open(self.pdf) as a, fitz.open(rendered) as b:
            images = [Image.open(io.BytesIO(d[0].get_pixmap(dpi=144).tobytes("png"))) for d in (a, b)]
            self.assertLess(sum(ImageStat.Stat(ImageChops.difference(*images)).mean) / 3, 2)
        deck = Presentation(self.pptx)
        slide = deck.slides[0]
        for shape in list(slide.shapes):
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE or (shape.shape_type == 1 and abs(shape.left.pt - 20) < .1):
                shape._element.getparent().remove(shape._element)
        deck.save(self.pptx)
        convert_office_pdf(self.pptx, rendered, lambda _: None)
        with fitz.open(rendered) as result:
            image = Image.open(io.BytesIO(result[0].get_pixmap(dpi=72).tobytes("png")))
            self.assertEqual(image.getpixel((45, 40)), (255, 255, 255))
            self.assertEqual(image.getpixel((280, 45)), (255, 255, 255))


if __name__ == "__main__":
    unittest.main()
