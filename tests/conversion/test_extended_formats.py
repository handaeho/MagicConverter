"""Regression coverage for the requested expanded offline format matrix."""

import csv
import gzip
import io
import json
import os
import plistlib
import subprocess
import sys
import tarfile
import tempfile
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageCms
from docx import Document
from openpyxl import Workbook, load_workbook
from pptx import Presentation
from pypdf import PdfReader, PdfWriter
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase

from magic_converter.core.conversion.converter import ConversionWorker, ConversionError, convert_image, convert_media, create_zip, find_ffmpeg
from magic_converter.core.documents.document_converter import html_to_pdf, pdf_text_pages, read_document
from magic_converter.core.conversion.extended_converter import convert_extended
from magic_converter.core.conversion.file_router import ALL_ACTIONS, slots_for
from magic_converter.core.engines.office_engine import presentation_available
from magic_converter.core.documents.spreadsheet_converter import read_sheets

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
for name in ("segoeui.ttf", "malgun.ttf"):
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))


class ExtendedFormatTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def convert(self, source, key):
        updates = []
        result = convert_extended(source, ALL_ACTIONS[key], updates.append)
        self.assertTrue(result.exists())
        return result

    def pdf(self):
        output = self.root / "two pages.pdf"
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first.pdf", Path(directory) / "second.pdf"
            html_to_pdf("<h1>Page One</h1><p>Hello PDF. 첫 번째 페이지.</p>", first)
            html_to_pdf("<h1>Page Two</h1><p>Second page content.</p>", second)
            writer = PdfWriter()
            writer.append(first)
            writer.append(second)
            writer.add_metadata({"/Author": "Private Author"})
            writer.write(output)
            writer.close()
        return output

    @unittest.skipUnless(os.name == "nt", "Windows offscreen font discovery")
    def test_cli_plain_text_pdf_embeds_readable_windows_fonts(self):
        import pymupdf

        expected = "한글 ABC 123 <literal> & 日本語\n    keep four leading spaces"
        inputs = []
        for encoding in ("utf-8", "utf-8-sig", "utf-16", "cp949"):
            source = self.root / f"{encoding}.txt"
            source.write_bytes(expected.replace("\n", "\r\n").encode(encoding))
            inputs.append(str(source))
        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        env.pop("QT_QPA_FONTDIR", None)
        base = [sys.executable, "-m", "magic_converter", "convert", *inputs,
                "--action", "document:pdf", "--data-dir", str(self.root / "profile")]
        outputs = self.root / "converted"
        result = subprocess.run([*base, "--output", str(outputs)], env=env,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=45)
        self.assertEqual(result.returncode, 0, result.stderr)
        pdfs = list(outputs.glob("*.pdf"))
        self.assertEqual(len(pdfs), len(inputs))
        for pdf in pdfs:
            with pymupdf.open(pdf) as document:
                self.assertIn(expected, document[0].get_text())

        empty_fonts = self.root / "no-fonts"
        empty_fonts.mkdir()
        env["QT_QPA_FONTDIR"] = str(empty_fonts)
        rejected = self.root / "rejected"
        result = subprocess.run([*base, "--output", str(rejected)], env=env,
                                capture_output=True, encoding="utf-8", errors="replace", timeout=45)
        self.assertEqual(result.returncode, 1, result.stderr)
        self.assertIn("PDF에 사용할 글꼴을 찾지 못했습니다", result.stdout)
        self.assertFalse(list(rejected.glob("*")))

    def test_all_format_families_route_and_engine_requirements_are_visible(self):
        for name in ("photo.heic", "photo.avif", "picture.svg", "sound.flac", "sound.opus", "video.wmv",
                     "file.pdf", "deck.pptx", "deck.ppt", "doc.docx", "book.epub", "table.ods",
                     "table.xls", "subtitle.srt", "archive.rar", "clip.textClipping"):
            with self.subTest(name=name):
                self.assertTrue(any(slot.enabled for slot in slots_for([name], "convert")))
        pdf = slots_for(["test.pdf"], "convert")
        self.assertEqual({slot.action.label for slot in pdf if slot.enabled}, {"DOCX", "PPTX (고정)", "PPTX (편집)", "XLSX", "JPG", "PNG", "EPUB", "TXT", "MD", "CBZ"})
        self.assertFalse(any(slot.action.label == "PAGES" for slot in pdf))
        self.assertFalse(any(slot.enabled for slot in slots_for(["deck.ppt"], "convert", presentation_available=False)))
        self.assertTrue(any(slot.enabled for slot in slots_for(["deck.pptx"], "convert", presentation_available=False)))

    def test_pdf_all_pages_300dpi_and_collision_safe_folder(self):
        source = self.pdf()
        existing = self.root / "two pages_converted"
        existing.mkdir()
        (existing / "keep.txt").write_text("keep")
        for key in ("document:png", "document:jpg"):
            output = self.convert(source, key)
            self.assertNotEqual(output, existing)
            images = list(output.glob("*"))
            self.assertEqual(len(images), 2)
            with Image.open(images[0]) as image:
                self.assertGreater(image.width, 2400)
                self.assertGreater(image.height, 3400)
                self.assertAlmostEqual(image.info["dpi"][0], 300, delta=0.2)
        self.assertEqual((existing / "keep.txt").read_text(), "keep")

    def test_pdf_docx_pptx_xlsx_and_text_exports(self):
        source = self.pdf()
        docx = self.convert(source, "document:docx")
        self.assertIn("Page Two", "\n".join(p.text for p in Document(docx).paragraphs))
        slides = Presentation(self.convert(source, "pptx_image"))
        self.assertEqual(len(slides.slides), 2)
        self.assertTrue(all(len(slide.shapes) == 1 for slide in slides.slides))
        xlsx = load_workbook(self.convert(source, "document:xlsx"))
        self.assertEqual(xlsx.sheetnames, ["Page 1", "Page 2"])
        self.assertIn("Page One", str(list(xlsx.worksheets[0].values)))
        xlsx.close()
        for suffix in ("txt", "md", "epub"):
            result = self.convert(source, f"document:{suffix}")
            if suffix == "epub":
                self.assertIn("Page Two", read_document(result)[0])
                with zipfile.ZipFile(result) as archive:
                    self.assertEqual(archive.namelist()[0], "mimetype")
                    for member in ("EPUB/package.opf", "EPUB/content.xhtml", "EPUB/nav.xhtml"):
                        ET.fromstring(archive.read(member))
            else:
                self.assertIn("Page Two", result.read_text(encoding="utf-8"))

    def test_worker_dispatches_pdf_and_rtfd_bundle_keeps_attachments(self):
        source = self.pdf()
        worker = ConversionWorker(99, (source,), ALL_ACTIONS["document:txt"], None)
        results = []
        worker.signals.finished.connect(results.append)
        worker.run()
        self.assertEqual(results[0].completed, 1)
        self.assertFalse(results[0].errors)
        self.assertIn("Page Two", results[0].outputs[0].read_text(encoding="utf-8"))
        bundle = self.root / "sample.rtfd"
        bundle.mkdir()
        (bundle / "TXT.rtf").write_text(r"{\rtf1\ansi Bundle body}")
        (bundle / "attachment.bin").write_bytes(b"attachment")
        output = self.convert(bundle, "document:txt")
        self.assertIn("Bundle body", output.read_text(encoding="utf-8"))
        with zipfile.ZipFile(create_zip((bundle,))) as archive:
            self.assertEqual(archive.read("sample.rtfd/attachment.bin"), b"attachment")

    def test_document_metadata_and_markdown_rtf_clipping(self):
        source = self.root / "document.md"
        source.write_text("# Heading\n\n한글 paragraph\n\n- first\n- second", encoding="utf-8")
        for key in ("document:docx", "document:txt", "document:html", "document:epub", "document:pdf"):
            output = self.convert(source, key)
            self.assertGreater(output.stat().st_size, 0)
        rtf = self.root / "sample.rtf"
        rtf.write_text(r"{\rtf1\ansi Hello \b World\b0\par second line}")
        self.assertIn("World", self.convert(rtf, "document:txt").read_text(encoding="utf-8"))
        clipping = self.root / "example.textClipping"
        clipping.write_bytes(plistlib.dumps({"public.utf8-plain-text": "선택한 텍스트"}))
        self.assertIn("선택한", self.convert(clipping, "document:txt").read_text(encoding="utf-8"))
        pdf = self.pdf()
        cleaned = self.convert(pdf, "strip")
        self.assertEqual(len(PdfReader(cleaned).pages), 2)
        self.assertNotIn("Private Author", str(PdfReader(cleaned).metadata))
        docx = Document()
        docx.add_paragraph("Keep body")
        docx.core_properties.author = "Private Author"
        original = self.root / "private.docx"
        docx.save(original)
        clean_docx = Document(self.convert(original, "strip"))
        self.assertEqual(clean_docx.paragraphs[0].text, "Keep body")
        self.assertFalse(clean_docx.core_properties.author)

    def test_heic_avif_svg_and_image_pdf(self):
        source = self.root / "image.png"
        Image.new("RGBA", (64, 48), (240, 100, 20, 140)).save(source)
        for suffix in (".heic", ".avif", ".pdf"):
            output = self.root / ("image" + suffix)
            convert_image(source, output, suffix)
            self.assertGreater(output.stat().st_size, 0)
            if suffix != ".pdf":
                with Image.open(output) as image:
                    self.assertEqual(image.size, (64, 48))
        svg = self.root / "input.svg"
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="80" height="40"><rect width="80" height="40" fill="orange"/></svg>')
        convert_image(svg, self.root / "svg.png", ".png")
        with Image.open(self.root / "svg.png") as image:
            self.assertEqual(image.size, (80, 40))

    def test_spreadsheet_sheets_roundtrip_and_multiline_csv(self):
        source = self.root / "book.xlsx"
        book = Workbook()
        book.active.title = "One"
        book.active.append(["한글", 42, True])
        book.create_sheet("Two").append(["line1\nline2", "comma,value"])
        book.save(source)
        ods = self.convert(source, "sheet:ods")
        self.assertEqual([name for name, rows in read_sheets(ods)], ["One", "Two"])
        self.assertEqual(read_sheets(ods)[0][1][0], ["한글", 42.0, True])
        csv_folder = self.convert(source, "sheet:csv")
        self.assertEqual(len(list(csv_folder.glob("*.csv"))), 2)
        second = next(path for path in csv_folder.glob("*Two.csv"))
        self.assertEqual(read_sheets(second)[0][1][0], ["line1\nline2", "comma,value"])
        xlsx = self.convert(ods, "sheet:xlsx")
        self.assertEqual(len(read_sheets(xlsx)), 2)
        self.assertTrue(self.convert(source, "document:pdf").stat().st_size)

    def test_png_preserves_original_bytes_and_native_gray_depth(self):
        source = self.root / "gray.png"
        gray = Image.new("I;16", (5, 1))
        gray.putdata([0, 256, 1024, 32768, 65535])
        gray.save(source, dpi=(144, 144))
        original = source.read_bytes()
        output = self.root / "copy.png"
        convert_image(source, output, ".png")
        self.assertEqual(output.read_bytes(), original)
        tiff = self.root / "gray.tiff"
        gray.save(tiff)
        convert_image(tiff, output, ".png")
        with Image.open(output) as converted:
            self.assertEqual(converted.mode, "I;16")
            self.assertEqual(converted.tobytes(), gray.tobytes())

    def test_jpeg_to_png_preserves_icc_and_normalizes_orientation(self):
        source = self.root / "rotated.jpg"
        output = self.root / "rotated.png"
        profile = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        exif = Image.Exif()
        exif[274] = 6
        exif[271] = "Test camera"
        Image.new("RGB", (16, 10), "green").save(source, icc_profile=profile, exif=exif, dpi=(120, 180))
        convert_image(source, output, ".png")
        with Image.open(output) as picture:
            self.assertEqual(picture.size, (10, 16))
            self.assertEqual(picture.info["icc_profile"], profile)
            self.assertAlmostEqual(picture.info["dpi"][0], 180, places=1)
            self.assertAlmostEqual(picture.info["dpi"][1], 120, places=1)
            self.assertNotIn(274, picture.getexif())
            self.assertEqual(picture.getexif()[271], "Test camera")

    def test_jpeg_aliases_use_the_existing_png_route(self):
        from magic_converter.core.conversion.builtin_converters import create_catalog
        from magic_converter.core.conversion.conversion_execution import execute_file
        from magic_converter.core.conversion.conversion_request import prepare_request

        for suffix in (".jpe", ".jfif"):
            with self.subTest(suffix=suffix):
                source = self.root / ("image" + suffix)
                Image.new("RGB", (12, 10), "red").save(source, format="JPEG")
                request = prepare_request([source], "image:png", create_catalog())
                result = execute_file(request, request.files[0], None, lambda _: None, lambda _: None)
                with Image.open(result) as picture:
                    self.assertEqual((picture.format, picture.size), ("PNG", (12, 10)))

    def test_delimited_strings_remain_literal_in_xlsx_and_ods(self):
        source = self.root / "literal.csv"
        source.write_text("=1+1,#N/A,0017\n", encoding="utf-8")
        for path in (source, self.convert(source, "sheet:ods")):
            with self.subTest(path=path):
                output = self.convert(path, "sheet:xlsx")
                book = load_workbook(output)
                try:
                    row = book.active[1]
                    self.assertEqual([(cell.value, cell.data_type) for cell in row],
                                     [("=1+1", "s"), ("#N/A", "s"), ("0017", "s")])
                finally:
                    book.close()

    def test_ods_repeated_blanks_and_local_formula_survive_xlsx(self):
        source = self.root / "coordinates.ods"
        xml = """<office:document-content xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
 xmlns:table="urn:oasis:names:tc:opendocument:xmlns:table:1.0"
 xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0"
 xmlns:calc="urn:oasis:names:tc:opendocument:xmlns:of:1.2">
 <office:body><office:spreadsheet><table:table table:name="Data">
 <table:table-row><table:table-cell office:value-type="float" office:value="3"/></table:table-row>
 <table:table-row-group>
 <table:table-row table:number-rows-repeated="3"><table:table-cell/></table:table-row>
 <table:table-row>
 <table:table-cell table:number-columns-repeated="2"/>
 <table:table-cell office:value-type="float" office:value="6" table:formula="calc:=[.$A$1]*2"/>
 <table:table-cell office:value-type="float" office:value="3" table:formula="calc:=SUM([.A1])"/>
 </table:table-row></table:table-row-group>
 <table:table-row table:number-rows-repeated="1048576"><table:table-cell/></table:table-row>
 </table:table></office:spreadsheet></office:body></office:document-content>"""
        with zipfile.ZipFile(source, "w") as package:
            package.writestr("content.xml", xml)
        warnings = []
        output = convert_extended(source, ALL_ACTIONS["sheet:xlsx"], lambda _: None, warnings.append)
        book = load_workbook(output)
        try:
            sheet = book.active
            self.assertEqual((sheet["C5"].value, sheet["C5"].data_type), ("=$A$1*2", "f"))
            self.assertEqual((sheet["D5"].value, sheet["D5"].data_type), ("calc:=SUM([.A1])", "s"))
            self.assertIsNone(sheet["A2"].value)
            self.assertEqual(sheet.max_row, 5)
        finally:
            book.close()
        self.assertTrue(any("Data!D5" in warning for warning in warnings), warnings)

    def test_subtitles_and_archives(self):
        source = self.root / "captions.srt"
        source.write_text("1\n00:00:01,000 --> 00:00:03,500\n<b>안녕하세요</b>\nsecond line\n\n2\n00:00:05,000 --> 00:00:07,000\nBye\n", encoding="utf-8")
        vtt = self.convert(source, "subtitle:vtt")
        self.assertIn("00:00:03.500", vtt.read_text(encoding="utf-8"))
        self.assertIn("second line", self.convert(vtt, "subtitle:srt").read_text(encoding="utf-8"))
        self.assertNotIn("<b>", self.convert(vtt, "subtitle:txt").read_text(encoding="utf-8"))
        original = self.root / "archive.zip"
        with zipfile.ZipFile(original, "w") as archive:
            archive.writestr("folder/한글.txt", "value".encode())
            archive.writestr("other.bin", b"\x00\x01")
        for key in ("archive:tar", "archive:gzip"):
            packed = self.convert(original, key)
            returned = self.convert(packed, "archive:zip")
            with zipfile.ZipFile(returned) as archive:
                self.assertEqual(archive.read("folder/한글.txt"), b"value")
        malicious = self.root / "unsafe.zip"
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("../escape.txt", "bad")
        with self.assertRaises(ConversionError):
            self.convert(malicious, "archive:tar")
        self.assertFalse((self.root / "unsafe_converted.tar").exists())

    def test_archive_conversion_preserves_member_metadata_and_payload(self):
        source = self.root / "metadata.zip"
        entry = zipfile.ZipInfo("folder/file.txt", (2021, 2, 4, 8, 10, 12))
        entry.create_system = 3
        entry.external_attr = 0o100755 << 16
        with zipfile.ZipFile(source, "w") as archive:
            archive.writestr(entry, b"payload")
        tar = self.convert(source, "archive:tar")
        with tarfile.open(tar) as archive:
            member = archive.getmember(entry.filename)
            self.assertEqual(member.mode, 0o755)
            self.assertEqual(archive.extractfile(member).read(), b"payload")
        returned = self.convert(tar, "archive:zip")
        with zipfile.ZipFile(returned) as archive:
            restored = archive.getinfo(entry.filename)
            self.assertEqual(restored.date_time, entry.date_time)
            self.assertEqual(restored.external_attr >> 16, 0o100755)

    def test_additional_tar_inputs_keep_files_and_use_the_archive_wheel(self):
        for compression in ("xz", "bz2", "zst"):
            with self.subTest(compression=compression):
                source = self.root / ("example.tar." + compression)
                payload = b"original archive contents"
                entry = tarfile.TarInfo("folder/원본.txt")
                entry.mode = 0o640
                entry.mtime = 1700000000
                entry.size = len(payload)
                with tarfile.open(source, "w:" + compression) as archive:
                    archive.addfile(entry, io.BytesIO(payload))
                before = source.read_bytes()
                self.assertEqual({slot.action.key for slot in slots_for([source], "convert") if slot.enabled},
                                 {"archive:zip", "archive:tar", "archive:gzip"})
                with zipfile.ZipFile(self.convert(source, "archive:zip")) as archive:
                    self.assertEqual(archive.read(entry.name), payload)
                    self.assertEqual(archive.getinfo(entry.name).external_attr >> 16 & 0o777, entry.mode)
                self.assertEqual(source.read_bytes(), before)

    @unittest.skipUnless(os.environ.get("MAGIC_CONVERTER_TEST_OFFICE") == "1" and presentation_available(), "Set MAGIC_CONVERTER_TEST_OFFICE=1 for actual Office export")
    def test_real_powerpoint_pdf_and_all_slide_images(self):
        source = self.root / "presentation.pptx"
        presentation = Presentation()
        for title in ("First slide", "Second slide"):
            slide = presentation.slides.add_slide(presentation.slide_layouts[0])
            slide.shapes.title.text = title
        presentation.save(source)
        output = self.convert(source, "document:pdf")
        self.assertEqual(len(PdfReader(output).pages), 2)
        self.assertIn("Second slide", "\n".join(pdf_text_pages(output)))
        images = self.convert(source, "document:png")
        self.assertEqual(len(list(images.glob("*.png"))), 2)


@unittest.skipUnless(find_ffmpeg(), "FFmpeg unavailable")
class ExtendedMediaTests(unittest.TestCase):
    def test_all_added_audio_and_video_outputs(self):
        executable = find_ffmpeg()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.mp4"
            subprocess.run([executable, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=0.4",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.4", "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)],
                           check=True, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            for ext in ("m4a", "wav", "flac", "ogg", "opus", "aiff", "wma", "mov", "mkv", "avi", "wmv"):
                with self.subTest(ext=ext):
                    target = root / ("output." + ext)
                    convert_media(source, target, ("audio:" if ext in {"m4a", "wav", "flac", "ogg", "opus", "aiff", "wma"} else "media:") + ext, executable)
                    self.assertGreater(target.stat().st_size, 0)


if __name__ == "__main__":
    unittest.main()
