"""Dispatch document, sheet, subtitle and archive work inside QRunnables."""

from __future__ import annotations

import shutil
import tempfile
import zipfile
from pathlib import Path
from typing import Callable
from xml.etree import ElementTree as ET

from magic_converter.core.archives.archive_converter import convert_archive
from magic_converter.core.conversion.converter import ConversionError, output_file, publish_folder, pdf_to_pptx_image, pdf_to_pptx_editable
from magic_converter.core.documents.document_converter import convert_text_document, pdf_to_docx, pdf_to_xlsx, render_pdf_pages
from magic_converter.core.conversion.file_router import Action, SHEET_EXTENSIONS, SLIDE_EXTENSIONS
from magic_converter.core.engines.office_engine import office_pdf
from magic_converter.core.documents.spreadsheet_converter import read_sheets, write_delimited_sheets, write_sheets
from magic_converter.core.media.subtitle_converter import convert_subtitle


def strip_document_metadata(source: Path, destination: Path) -> None:
    if source.suffix.lower() == ".pdf":
        from pypdf import PdfWriter
        writer = PdfWriter(clone_from=str(source))
        writer.metadata = None
        if "/Metadata" in writer.root_object:
            del writer.root_object["/Metadata"]
        writer.write(destination)
        writer.close()
        return
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as output:
        for entry in original.infolist():
            data = original.read(entry)
            if entry.filename in {"docProps/core.xml", "docProps/custom.xml"}:
                root = ET.fromstring(data)
                for child in list(root):
                    root.remove(child)
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            elif entry.filename == "docProps/app.xml":
                root = ET.fromstring(data)
                for child in root:
                    if child.tag.rsplit("}", 1)[-1] in {"Company", "Manager"}:
                        child.text = ""
                data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
            output.writestr(entry, data)


def convert_extended(source: Path, action: Action, progress: Callable[[int | None], None],
                     warning: Callable[[str], None] = lambda _: None, *,
                     output_source: Path | None = None) -> Path:
    progress(0)
    anchor = output_source or source
    ext, suffix, key = source.suffix.lower(), action.suffix or source.suffix.lower(), action.key
    if key == "office:xlsx_print_pdf":
        from magic_converter.core.engines.hwp_converter import run_com
        if ext != ".xlsx":
            raise ConversionError("Excel 인쇄 PDF는 XLSX만 지원합니다.")
        progress(None)
        with output_file(anchor, ".pdf") as (temporary, destination):
            run_com(source, temporary, "excel_print")
        warning("Excel 인쇄 영역과 반복 제목 및 페이지 설정을 적용해 PDF를 출력했습니다.")
        progress(100)
        return destination
    from magic_converter.core.engines.office_formats import VARIANTS, canonical, normalize
    if ext in VARIANTS:
        with tempfile.TemporaryDirectory(prefix="magic-office-input-") as directory:
            standard = Path(directory) / ("document" + canonical(ext))
            normalize(source, standard)
            warning("Office 템플릿·매크로 문서를 표준 OOXML로 읽었습니다. 매크로·ActiveX·포함 개체는 결과에 포함되지 않습니다.")
            return convert_extended(standard, action, progress, warning, output_source=anchor)
    if key == "office:pptx":
        with output_file(anchor, ".pptx") as (temporary, destination):
            normalize(source, temporary)
        progress(100)
        return destination
    if ext == ".xlsx" and key == "sheet:xlsx":
        from openpyxl import load_workbook
        from magic_converter.core.engines.hwp_converter import audit_output
        with output_file(anchor, ".xlsx") as (temporary, destination):
            workbook = load_workbook(source, read_only=False, data_only=False, keep_links=False)
            try:
                workbook.template = False
                workbook.save(temporary)
            finally:
                workbook.close()
            audit_output(source, temporary, warning)
        progress(100)
        return destination
    if ext == ".docx" and key == "document:docx":
        from magic_converter.core.engines.hwp_converter import read_content, audit_output, validate_output
        from magic_converter.core.documents.hwp_documents import write_docx
        with output_file(anchor, suffix) as (temporary, destination):
            data = read_content(source)
            write_docx(data, temporary)
            validate_output(temporary)
            audit_output(source, temporary, warning, data)
        progress(100)
        return destination
    if ext in {".hwp", ".hwpx"} or key in {"document:hwp", "document:hwpx"}:
        from magic_converter.core.engines.hwp_converter import convert_hwp
        with output_file(anchor, suffix) as (temporary, destination):
            convert_hwp(source, temporary, progress, warning)
        return destination
    if key == "document:pdf" and ext in {".docx", ".pptx", ".xlsx"}:
        from magic_converter.core.engines.hwp_converter import convert_office_pdf
        with output_file(anchor, ".pdf") as (temporary, destination):
            convert_office_pdf(source, temporary, progress, warning)
        return destination
    if key in {"pptx_image", "pptx_editable"}:
        tag = "slide" if key == "pptx_image" else "editable"
        with output_file(anchor, ".pptx", tag=tag) as (temporary, destination):
            if key == "pptx_image":
                pdf_to_pptx_image(source, temporary, progress)
            else:
                pdf_to_pptx_editable(source, temporary, progress, warning)
        return destination
    if key in {"document:jpg", "document:png"}:
        if ext == ".pdf":
            return publish_folder(anchor, lambda folder: render_pdf_pages(source, folder, suffix, progress))
        with tempfile.TemporaryDirectory(prefix="magic-slides-") as directory:
            pdf = Path(directory) / "slides.pdf"
            progress(None)
            office_pdf(source, pdf)
            return publish_folder(anchor, lambda folder: render_pdf_pages(pdf, folder, suffix, progress))
    if key.startswith("sheet:") or (key == "document:pdf" and ext in SHEET_EXTENSIONS):
        sheets = read_sheets(source)
        progress(30)
        if suffix in {".csv", ".tsv"} and len(sheets) > 1:
            return publish_folder(anchor, lambda folder: write_delimited_sheets(sheets, folder, suffix))
        with output_file(anchor, suffix) as (temporary, destination):
            write_sheets(sheets, temporary, suffix)
        progress(100)
        return destination
    with output_file(anchor, suffix) as (temporary, destination):
        if key == "strip":
            strip_document_metadata(source, temporary)
        elif key.startswith("subtitle:"):
            convert_subtitle(source, temporary, suffix, notice=warning)
        elif key.startswith("archive:"):
            convert_archive(source, temporary, suffix)
        elif ext in SLIDE_EXTENSIONS:
            progress(None)
            office_pdf(source, temporary)
        elif ext == ".pdf" and suffix == ".docx":
            pdf_to_docx(source, temporary, progress, warning)
        elif ext == ".pdf" and suffix == ".xlsx":
            pdf_to_xlsx(source, temporary, progress)
        else:
            if ext == ".docx":
                from magic_converter.core.engines.hwp_converter import read_content
                from magic_converter.core.documents.document_audit import inventory
                data = read_content(source)
                for message in data.warnings:
                    warning(message)
                if suffix in {".txt", ".md", ".epub"}:
                    warning("텍스트 중심 출력입니다. 그림·표 배치·수식 조판은 포함하지 않습니다.")
                elif inventory(data)["equations"]:
                    warning("HTML 수식은 스크립트 텍스트로 출력합니다.")
            convert_text_document(source, temporary, suffix, progress)
    progress(100)
    return destination
