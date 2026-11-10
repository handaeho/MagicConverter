"""PDF rendering and text-oriented document interchange, entirely local."""

from __future__ import annotations

import html
from datetime import datetime, timezone
import io
import plistlib
import re
import shutil
import tempfile
import uuid
import zipfile
from contextlib import closing
from pathlib import Path, PurePosixPath
from typing import Callable
from xml.etree import ElementTree as ET

import markdown
import pypdfium2 as pdfium
from bs4 import BeautifulSoup
from docx import Document
from docx.shared import Inches
from PIL import Image
from striprtf.striprtf import rtf_to_text

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.engines.office_engine import find_libreoffice, find_office, office_pdf
from magic_converter.core.jobs.job_control import CancellableRLock, checked, check_cancelled
from magic_converter.core.documents.text_codec import decode_text

# PDFium is not thread safe, even with separate documents. Both QRunnables
# share this lock; Pillow, FFmpeg and the rest of the GUI remain independent.
PDFIUM_LOCK = CancellableRLock()
Progress = Callable[[int | None], None]


def pdf_text_pages(source: Path) -> list[str]:
    with PDFIUM_LOCK, pdfium.PdfDocument(str(source)) as document:
        pages: list[str] = []
        for index in checked(range(len(document))):
            with closing(document[index]) as page, closing(page.get_textpage()) as text:
                pages.append(text.get_text_range().replace("\r\n", "\n"))
        return pages


def render_pdf_pages(source: Path, directory: Path, suffix: str, progress: Progress) -> None:
    with PDFIUM_LOCK, pdfium.PdfDocument(str(source)) as document:
        count = len(document)
        if not count:
            raise ConversionError("PDF에 페이지가 없습니다.")
        for index in checked(range(count)):
            with closing(document[index]) as page, closing(page.render(scale=300 / 72)) as bitmap:
                image = bitmap.to_pil().convert("RGB")
                try:
                    options = {"quality": 94} if suffix == ".jpg" else {}
                    image.save(directory / f"page_{index + 1:04d}{suffix}", dpi=(300, 300), **options)
                finally:
                    image.close()
            progress((index + 1) * 100 // count)


def pdf_to_docx(source: Path, destination: Path, progress: Progress,
                warning: Callable[[str], None] = lambda _: None) -> None:
    from magic_converter.core.pdf.pdf_docx import convert_pdf_docx
    convert_pdf_docx(source, destination, progress, warning)


def pdf_to_xlsx(source: Path, destination: Path, progress: Progress) -> None:
    import pdfplumber
    from openpyxl import Workbook
    output = Workbook()
    output.remove(output.active)
    content_found = False
    with pdfplumber.open(source) as document:
        for index, page in enumerate(checked(document.pages)):
            page = page.dedupe_chars(tolerance=1)
            sheet = output.create_sheet(f"Page {index + 1}")
            tables = page.extract_tables()
            if tables:
                for table in tables:
                    for row in table:
                        sheet.append([cell or "" for cell in row])
                        content_found |= any(cell for cell in row)
                    sheet.append([])
            else:
                for line in (page.extract_text() or "").splitlines():
                    sheet.append([line])
                    content_found |= bool(line.strip())
            sheet.column_dimensions["A"].width = 70 if not tables else 24
            progress((index + 1) * 100 // max(1, len(document.pages)))
    if not content_found:
        raise ConversionError("PDF에 추출할 표/텍스트가 없습니다. 스캔 문서 OCR은 지원하지 않습니다.")
    output.save(destination)


def text_html(text: str, title: str = "") -> str:
    body = "".join(f"<p>{html.escape(paragraph).replace(chr(10), '<br/>')}</p>" for paragraph in text.split("\n\n"))
    return f"<!doctype html><html><head><meta charset='utf-8'><title>{html.escape(title)}</title></head><body>{body}</body></html>"


def read_document(source: Path) -> tuple[str, str]:
    ext = source.suffix.lower()
    if ext == ".pdf":
        text = "\n\n".join(pdf_text_pages(source))
        if not text.strip():
            raise ConversionError("추출 가능한 텍스트가 없습니다. 스캔 문서 OCR은 지원하지 않습니다.")
        return text, text_html(text, source.stem)
    if ext == ".docx":
        from magic_converter.core.documents.hwp_documents import read_docx, document_html
        data = read_docx(source)
        return data.text(), "<html><meta charset='utf-8'><body>" + document_html(data) + "</body></html>"
    if ext == ".epub":
        with zipfile.ZipFile(source) as archive:
            container = ET.fromstring(archive.read("META-INF/container.xml"))
            rootfile = next(element.attrib["full-path"] for element in container.iter() if element.tag.endswith("rootfile"))
            package = ET.fromstring(archive.read(rootfile))
            manifest = {element.attrib["id"]: element.attrib for element in package.iter() if element.tag.endswith("item")}
            chunks: list[str] = []
            for reference in checked(package.iter()):
                if not reference.tag.endswith("itemref"):
                    continue
                item = manifest[reference.attrib["idref"]]
                path = str(PurePosixPath(rootfile).parent / item["href"])
                chunks.append(archive.read(path).decode("utf-8", "replace"))
        soup = BeautifulSoup("\n".join(chunks), "html.parser")
        for tag in soup(["script", "style", "head"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        return text, text_html(text, source.stem)
    if ext == ".rtfd":
        if source.is_dir():
            rtf = next((path for path in source.iterdir() if path.suffix.lower() == ".rtf"), None)
            if rtf is None:
                raise ConversionError("RTFD 번들에 RTF 문서가 없습니다.")
            return read_document(rtf)
        with zipfile.ZipFile(source) as archive:
            rtf = next(name for name in archive.namelist() if name.lower().endswith(".rtf"))
            text = rtf_to_text(archive.read(rtf).decode("utf-8", "replace"), errors="ignore")
        return text, text_html(text)
    data = source.read_bytes()
    if ext == ".textclipping":
        try:
            value = plistlib.loads(data)
            if isinstance(value, dict):
                value = next(value[key] for key in ("public.utf8-plain-text", "NSStringPboardType", "utf8", "text") if key in value)
            text = value.decode("utf-8") if isinstance(value, bytes) else str(value)
        except (ValueError, KeyError, StopIteration, UnicodeError, plistlib.InvalidFileException) as exc:
            raise ConversionError("이 textClipping은 macOS 리소스 포크가 필요합니다. TXT/RTF로 저장한 뒤 변환하세요.") from exc
    else:
        text = decode_text(data)
    if ext == ".rtf":
        text = rtf_to_text(text, errors="ignore")
    if ext in {".md", ".markdown"}:
        rendered = markdown.markdown(text, extensions=["tables", "fenced_code"])
        return BeautifulSoup(rendered, "html.parser").get_text("\n"), "<html><meta charset='utf-8'><body>" + rendered + "</body></html>"
    return text, text_html(text, source.stem)


def write_epub(text: str, destination: Path, title: str) -> None:
    identifier = str(uuid.uuid4())
    modified = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    body = text_html(text, title)
    body = body.replace("<!doctype html>", "").replace("<html>", '<html xmlns="http://www.w3.org/1999/xhtml">').replace("<meta charset='utf-8'>", '<meta charset="utf-8"/>')
    container = '<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    package = f'''<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/"><dc:identifier id="id">urn:uuid:{identifier}</dc:identifier><dc:title>{html.escape(title)}</dc:title><dc:language>ko</dc:language><meta property="dcterms:modified">{modified}</meta></metadata><manifest><item id="content" href="content.xhtml" media-type="application/xhtml+xml"/><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/></manifest><spine><itemref idref="content"/></spine></package>'''
    navigation = '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"><head><title>Contents</title></head><body><nav epub:type="toc"><ol><li><a href="content.xhtml">Document</a></li></ol></nav></body></html>'
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_STORED)
        for name, content in (("META-INF/container.xml", container), ("EPUB/package.opf", package), ("EPUB/content.xhtml", body), ("EPUB/nav.xhtml", navigation)):
            archive.writestr(name, content.encode("utf-8"))


def html_to_pdf(content: str, destination: Path, landscape: bool = False) -> None:
    check_cancelled()
    from PySide6.QtCore import QMarginsF
    from PySide6.QtGui import QPageLayout, QPageSize, QPdfWriter, QTextDocument
    writer = QPdfWriter(str(destination))
    writer.setResolution(300)
    writer.setPageSize(QPageSize(QPageSize.PageSizeId.A4))
    if landscape:
        writer.setPageOrientation(QPageLayout.Orientation.Landscape)
    writer.setPageMargins(QMarginsF(12, 12, 12, 12), QPageLayout.Unit.Millimeter)
    document = QTextDocument()
    document.setDefaultStyleSheet("body {font-family:'Malgun Gothic'; font-size:10pt;} table {border-collapse:collapse;} td,th {border:1px solid #888;padding:4px;} p {margin:6px 0;}")
    document.setHtml(content)
    document.print_(writer)
    del writer
    check_cancelled()


def convert_text_document(source: Path, destination: Path, suffix: str, progress: Progress) -> None:
    if suffix == ".pdf" and source.suffix.lower() == ".docx" and (find_office("Word") or find_libreoffice()):
        office_pdf(source, destination, "Word")
        progress(100)
        return
    text, content = read_document(source)
    progress(40)
    if suffix in {".txt", ".md"}:
        if source.suffix.lower() in {".md", ".markdown"} and suffix == ".md":
            text = decode_text(source.read_bytes())
        destination.write_text(text, encoding="utf-8")
    elif suffix == ".html":
        destination.write_text(content, encoding="utf-8")
    elif suffix == ".pdf":
        html_to_pdf(content, destination)
    elif suffix == ".epub":
        write_epub(text, destination, source.stem)
    elif suffix == ".docx":
        output = Document()
        soup = BeautifulSoup(content, "html.parser")
        for element in checked(soup.find_all(["p", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr", "pre"])):
            value = element.get_text(" ")
            if element.name.startswith("h"):
                output.add_heading(value, level=int(element.name[1]))
            elif element.name == "li":
                output.add_paragraph(value, style="List Bullet")
            else:
                output.add_paragraph(value)
        output.save(destination)
    else:
        raise ConversionError(f"지원하지 않는 문서 출력: {suffix}")
    progress(100)
