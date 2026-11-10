
import hashlib
import json
import os
import struct
import subprocess
import sys
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path
from unittest.mock import MagicMock, patch
from xml.etree import ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from docx import Document
from docx.shared import Pt, RGBColor
from openpyxl import Workbook
from pptx import Presentation
from PySide6.QtWidgets import QApplication

from magic_converter.core.conversion.converter import ConversionError, ConversionWorker
from magic_converter.core.conversion.extended_converter import convert_extended
from magic_converter.core.conversion.file_router import ALL_ACTIONS, slots_for
from magic_converter.core.engines.hwp_converter import (convert_hwp, convert_office_pdf, FALLBACK_MESSAGE,
                                                   office_content, validate_output)
from magic_converter.core.documents.hwp_documents import (DocumentData, Paragraph, TextRun, Table, Cell, read_docx,
    read_hwpx, read_hwp, write_docx, write_hwpx, decode_hwp_text, hwp_records, q)

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def make_hwp(path, text="한글 원본 본문", compressed=True, encrypted=False):
    """Small real CFB v3 container; no olefile writer/mocked streams required."""
    free, end = 0xFFFFFFFF, 0xFFFFFFFE
    header = bytearray(512)
    header[:8] = bytes.fromhex("D0CF11E0A1B11AE1")
    struct.pack_into("<HHHHH", header, 24, 0x3E, 3, 0xFFFE, 9, 6)
    struct.pack_into("<IIIIIIIII", header, 40, 0, 1, 1, 0, 4096, end, 0, end, 0)
    struct.pack_into("<109I", header, 76, 0, *([free] * 108))
    fat = [free] * 128
    fat[0], fat[1] = 0xFFFFFFFD, end
    for start in (2, 10):
        for i in range(start, start + 7):
            fat[i] = i + 1
        fat[start + 7] = end
    def entry(name, kind, child=free, right=free, start=end, size=0):
        value = bytearray(128)
        encoded = (name + "\0").encode("utf-16le")
        value[:len(encoded)] = encoded
        struct.pack_into("<HBBIII", value, 64, len(encoded), kind, 1, free, right, child)
        struct.pack_into("<IQ", value, 116, start, size)
        return value
    directory = (entry("Root Entry", 5, child=1) + entry("FileHeader", 2, right=2, start=2, size=4096)
                 + entry("BodyText", 1, child=3) + entry("Section0", 2, start=10, size=4096))
    info = bytearray(4096)
    info[:17] = b"HWP Document File"
    struct.pack_into("<I", info, 32, 0x05000000)
    struct.pack_into("<I", info, 36, int(compressed) | (2 if encrypted else 0))
    payload = (text + "\r").encode("utf-16le")
    records = struct.pack("<I", 67 | len(payload) << 20) + payload
    if compressed:
        encoder = zlib.compressobj(wbits=-15)
        records = encoder.compress(records) + encoder.flush()
    else:
        padding = 4096 - len(records) - 4
        records += struct.pack("<I", padding << 20) + b"\0" * padding
    path.write_bytes(header + struct.pack("<128I", *fat) + directory + info + records.ljust(4096, b"\0"))




if __name__ == "__main__":
    unittest.main()
