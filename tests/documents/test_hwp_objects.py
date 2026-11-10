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
from magic_converter.core.documents.hwp_documents import (Cell, DocumentData, Paragraph, Picture, Table, TextRun,
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
        pass
        with fitz.open(output) as pdf:
            self.assertGreaterEqual(sum(len(p.get_images()) for p in pdf), 1)
            self.assertIn("Merged", "".join(p.get_text() for p in pdf))
        self.assertTrue(notices)

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
