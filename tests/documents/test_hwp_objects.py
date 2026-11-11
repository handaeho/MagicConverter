"""Pictures, nested tables, binary record levels, and the supplied regressions."""

import hashlib
import io
import os
from pathlib import Path
import struct
import tempfile
import unittest
from unittest.mock import patch
import zipfile
import zlib
from xml.etree import ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PIL import Image
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase
import pymupdf as fitz

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.conversion.extended_converter import convert_extended
from magic_converter.core.conversion.file_router import ALL_ACTIONS
from magic_converter.core.documents.hwp_documents import (Border, Cell, DocumentData, InlineLine, Paragraph, Picture, Table, TextRun,
                                                   read_hwp, read_hwpx, read_docx, write_hwpx, write_docx)
from magic_converter.core.documents.rich_hwp import object_counts, walk_blocks

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
for name in ("segoeui.ttf", "malgun.ttf"):
    font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
    if font.is_file():
        QFontDatabase.addApplicationFont(str(font))
WORKSPACE = Path(__file__).resolve().parents[2]
USER_HWP = WORKSPACE / "상주시_AnyArchive_Arch-OCR_설치결과서.hwp"
USER_HWPX = WORKSPACE / "제안서 협상결과 통보서.hwpx"


def png():
    out = io.BytesIO()
    Image.new("RGB", (320, 160), (245, 132, 17)).save(out, "PNG")
    return out.getvalue()


def binary_table_picture(path: Path, storage_id=10):
    """Real CFB with a nested picture control; BinData ID uses hexadecimal."""
    def record(tag, level, data):
        return struct.pack("<I", tag | level << 10 | len(data) << 20) + data
    def marker(identifier):
        return b"\x0b\x00" + identifier + b"\0" * 10
    picture = bytearray(78)
    struct.pack_into("<4i", picture, 44, 0, 0, 24000, 12000)
    struct.pack_into("<H", picture, 71, storage_id)
    shape = bytearray(80)
    shape[:8] = b"cip$cip$"
    struct.pack_into("<II", shape, 28, 14400, 7200)
    cell = bytearray(47)
    struct.pack_into("<4H2I", cell, 8, 0, 0, 1, 1, 21600, 10000)
    table = bytearray(22)
    struct.pack_into("<HH", table, 4, 1, 1)
    section = b"".join((record(66, 0, b"\0" * 24), record(67, 1, marker(b" lbt") + b"\r\0"),
        record(71, 1, b" lbt" + b"\0" * 44), record(77, 2, bytes(table)), record(72, 2, bytes(cell)),
        record(66, 2, b"\0" * 24), record(67, 3, "Cell ".encode("utf-16le") + marker(b" osg") + b"\r\0"),
        record(71, 3, b" osg" + b"\0" * 44), record(76, 4, bytes(shape)), record(85, 5, bytes(picture))))
    encoder = zlib.compressobj(wbits=-15)
    section = (encoder.compress(section) + encoder.flush()).ljust(4096, b"\0")
    file_header = bytearray(4096)
    file_header[:17] = b"HWP Document File"
    struct.pack_into("<I", file_header, 32, 0x05000000)
    struct.pack_into("<I", file_header, 36, 1)
    image = png().ljust(4096, b"\0")
    free, end = 0xFFFFFFFF, 0xFFFFFFFE
    header = bytearray(512)
    header[:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<9I", header, 40, 0, 1, 1, 0, 4096, end, 0, end, 0)
    struct.pack_into("<109I", header, 76, 0, *([free] * 108))
    fat = [free] * 128
    fat[0], fat[1], fat[2] = 0xFFFFFFFD, 2, end
    for start in (3, 11, 19):
        for index in range(start, start + 7):
            fat[index] = index + 1
        fat[start + 7] = end
    def entry(name, kind, child=free, right=free, start=end, size=0):
        value = bytearray(128)
        encoded = (name + "\0").encode("utf-16le")
        value[:len(encoded)] = encoded
        struct.pack_into("<HBBIII", value, 64, len(encoded), kind, 1, free, right, child)
        struct.pack_into("<IQ", value, 116, start, size)
        return value
    directory = b"".join((entry("Root Entry", 5, child=1), entry("FileHeader", 2, right=2, start=3, size=4096),
        entry("BodyText", 1, right=4, child=3), entry("Section0", 2, start=11, size=4096),
        entry("BinData", 1, child=5), entry("BIN000A.png", 2, start=19, size=4096)))
    path.write_bytes(header + struct.pack("<128I", *fat) + directory.ljust(1024, b"\0") + file_header + section + image)
    return image


class HwpObjectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def fixture(self):
        picture = Picture(png(), 144, 72, "source.png", (0, .1, 0, 0))
        inner = Table(2, 2, [Cell(0, 0, [Paragraph([TextRun("Merged")])], 1, 2),
                              Cell(1, 0, [Paragraph([TextRun("Cell")])]), Cell(1, 1, [Paragraph([picture])])], [90, 90])
        outer = Table(1, 1, [Cell(0, 0, [Paragraph([TextRun("Before")]), inner, Paragraph([TextRun("After")])])], [216])
        return DocumentData([Paragraph([TextRun("Title", 14, True)], "center"), outer],
                             headers=[Table(1, 1, [Cell(0, 0, [Paragraph([TextRun("Header")])])])])

    def test_binary_levels_table_image_and_hexadecimal_asset_reference(self):
        source = self.root / "nested.hwp"
        image = binary_table_picture(source)
        data = read_hwp(source)
        self.assertEqual(object_counts(data), {"tables": 1, "pictures": 1})
        picture = next(b for b in walk_blocks(data.blocks) if isinstance(b, Picture))
        self.assertEqual(picture.data, image)
        self.assertEqual((picture.width, picture.height), (144, 72))
        self.assertIn("Cell", data.text())

    def test_missing_binary_picture_fails_instead_of_dropping_it(self):
        source = self.root / "missing.hwp"
        binary_table_picture(source, storage_id=11)
        with self.assertRaisesRegex(ConversionError, "그림 데이터"):
            read_hwp(source)

    def test_nested_hwpx_docx_roundtrip_keeps_grid_crop_and_image_bytes(self):
        data = self.fixture()
        for suffix, write, read in (("hwpx", write_hwpx, read_hwpx), ("docx", write_docx, read_docx)):
            with self.subTest(suffix=suffix):
                path = self.root / ("nested." + suffix)
                write(data, path)
                reread = read(path)
                self.assertEqual(object_counts(reread), {"tables": 3, "pictures": 1})
                for text in ("Title", "Header", "Merged", "Cell", "Before", "After"):
                    self.assertIn(text, reread.text())
                picture = next(b for b in walk_blocks(reread.blocks) if isinstance(b, Picture))
                self.assertEqual(picture.data, png())
                self.assertAlmostEqual(picture.crop[1], .1, places=4)
                tables = [b for b in walk_blocks(reread.blocks) if isinstance(b, Table)]
                self.assertTrue(any(c.colspan == 2 for t in tables for c in t.cells))
                self.assertEqual(reread.blocks[0].alignment, "center")

    def test_offline_pdf_fallback_really_embeds_picture(self):
        source = self.root / "nested.hwpx"
        write_hwpx(self.fixture(), source)
        notices = []
        with patch("magic_converter.core.engines.rhwp_engine.locate", return_value=None), patch(
                "magic_converter.core.engines.hwp_converter.com_available", return_value=False):
            output = convert_extended(source, ALL_ACTIONS["document:pdf"], lambda _: None, notices.append)
        with fitz.open(output) as pdf:
            self.assertGreaterEqual(sum(len(p.get_images()) for p in pdf), 1)
            self.assertIn("Merged", "".join(p.get_text() for p in pdf))
        self.assertTrue(notices)

    def test_word_body_height_and_inherited_font_survive_hwpx_export(self):
        from docx import Document
        from docx.shared import Pt

        doc = Document()
        section = doc.sections[0]
        section.top_margin, section.bottom_margin = Pt(49), Pt(14)
        doc.styles["Normal"].font.size = Pt(19)
        doc.styles["Normal"].font.name = "Batang"
        paragraph = doc.add_paragraph("Title")
        paragraph.paragraph_format.line_spacing = Pt(24)
        source, output = self.root / "layout.docx", self.root / "layout.hwpx"
        doc.save(source)
        write_hwpx(read_docx(source), output)
        with zipfile.ZipFile(output) as package:
            page = ET.fromstring(package.read("Contents/section0.xml")).find(".//{*}pagePr")
            margins = page.find("{*}margin")
            self.assertEqual([margins.get(key) for key in ("top", "bottom", "header", "footer")],
                             ["4900", "1400", "0", "0"])
        title = read_hwpx(output).blocks[0]
        self.assertEqual((title.runs[0].size, title.runs[0].font, title.line_spacing),
                         (19, "Batang", ("FIXED", 24)))

    def test_hwpx_late_footer_keeps_only_source_border_and_live_page_number(self):
        from docx import Document
        from docx.oxml.ns import qn

        footer = Table(1, 1, [Cell(0, 0, [Paragraph([TextRun("1", page_number=True)])],
                                   borders=(Border(), Border(), Border("single", 1), Border()))])
        cover = Paragraph([TextRun("Cover")], borders=(Border(), Border(), Border(), Border("single", 2)))
        data = DocumentData([cover, Paragraph([TextRun("Main")], page_break_before=True)],
                            footers=[footer], header_footer_start=1, page_number_start=1)
        source, output = self.root / "footer.hwpx", self.root / "footer.docx"
        write_hwpx(data, source)
        write_docx(read_hwpx(source), output)
        doc = Document(output)
        border = doc.paragraphs[0]._p.pPr.find(qn("w:pBdr")).find(qn("w:bottom"))
        self.assertEqual(border.get(qn("w:sz")), "16")
        self.assertEqual(len(doc.sections), 2)
        self.assertFalse(doc.sections[0].footer._has_definition)
        section = doc.sections[1]
        self.assertEqual(section._sectPr.find(qn("w:pgNumType")).get(qn("w:start")), "1")
        cell = section.footer.tables[0].cell(0, 0)
        edges = cell._tc.tcPr.find(qn("w:tcBorders"))
        self.assertEqual({node.tag: node.get(qn("w:val")) for node in edges},
                         {qn("w:" + name): style for name, style in
                          (("top", "single"), ("left", "nil"), ("bottom", "nil"), ("right", "nil"))})
        self.assertEqual(cell._tc.xpath(".//w:fldSimple")[0].get(qn("w:instr")), "PAGE")

    def test_hwpx_picture_crop_uses_image_dimensions_not_shape_size(self):
        original, adjusted = self.root / "shape.hwpx", self.root / "different-image-size.hwpx"
        write_hwpx(self.fixture(), original)
        with zipfile.ZipFile(original) as archive, zipfile.ZipFile(adjusted, "w") as output:
            for name in archive.namelist():
                data = archive.read(name)
                if name == "Contents/section0.xml":
                    root = ET.fromstring(data)
                    picture = root.find(".//{*}pic")
                    original_size = picture.find("{*}orgSz")
                    original_size.set("height", str(2 * int(original_size.get("height"))))
                    data = ET.tostring(root)
                output.writestr(name, data)
        picture = next(block for block in walk_blocks(read_hwpx(adjusted).blocks) if isinstance(block, Picture))
        self.assertAlmostEqual(picture.crop[1], .1, places=4)

    def test_word_group_in_compatibility_choice_survives_as_editable_line(self):
        from docx import Document
        from lxml import etree

        line = InlineLine(510.1, .6, 0, 510.09, .3, .6)
        word, hwpx, returned = (self.root / name for name in ("line.docx", "line.hwpx", "returned.docx"))
        write_docx(DocumentData([Paragraph([line])], margins=(39, 39, 49, 14)), word)
        document = Document(word)
        drawing = document.element.xpath(".//w:drawing")[0]
        mc = "{http://schemas.openxmlformats.org/markup-compatibility/2006}"
        alternate = etree.Element(mc + "AlternateContent", nsmap={
            "wpg": "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"})
        drawing.addprevious(alternate)
        etree.SubElement(alternate, mc + "Choice", Requires="wpg").append(drawing)
        etree.SubElement(alternate, mc + "Fallback")
        document.save(word)
        model = read_docx(word)
        self.assertEqual(model.blocks[0].runs, [line])
        write_hwpx(model, hwpx)
        with zipfile.ZipFile(hwpx) as package:
            root = ET.fromstring(package.read("Contents/section0.xml"))
            self.assertEqual(len(root.findall(".//{*}line")), 1)
            self.assertFalse(root.findall(".//{*}pic"))
        write_docx(read_hwpx(hwpx), returned)
        self.assertEqual(read_docx(returned).blocks[0].runs, [line])

    @unittest.skipUnless(USER_HWP.exists() and USER_HWPX.exists(), "User-provided regression documents are local only")
    def test_supplied_documents_keep_every_table_and_picture_without_modifying_originals(self):
        for source, read, expected in ((USER_HWP, read_hwp, {"tables": 9, "pictures": 4}),
                                        (USER_HWPX, read_hwpx, {"tables": 2, "pictures": 0})):
            with self.subTest(name=source.name):
                before = hashlib.sha256(source.read_bytes()).hexdigest()
                data = read(source)
                self.assertEqual(object_counts(data), expected)
                expected_images = sorted(hashlib.sha256(b.data).hexdigest()
                    for b in walk_blocks([*data.headers, *data.blocks, *data.footers]) if isinstance(b, Picture))
                for suffix, writer, reader in (("hwpx", write_hwpx, read_hwpx), ("docx", write_docx, read_docx)):
                    target = self.root / (source.stem + "." + suffix)
                    writer(data, target)
                    restored = reader(target)
                    self.assertEqual(object_counts(restored), expected)
                    images = sorted(hashlib.sha256(b.data).hexdigest()
                        for b in walk_blocks([*restored.headers, *restored.blocks, *restored.footers]) if isinstance(b, Picture))
                    self.assertEqual(images, expected_images)
                self.assertEqual(before, hashlib.sha256(source.read_bytes()).hexdigest())


if __name__ == "__main__":
    unittest.main()
