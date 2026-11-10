"""PDF to native Word paragraphs, merged tables and displayed pictures."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Callable

import pymupdf as fitz
from docx import Document
from docx.enum.section import WD_SECTION_START
from docx.enum.table import WD_ROW_HEIGHT_RULE, WD_TABLE_ALIGNMENT
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt, RGBColor

from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.pdf.pdf_layout import (PYMUPDF_LOCK, PdfCell, PdfTable, cell_alignment, cell_margins,
                         contains_center, extract_tables, font_name, lines, pictures,
                         assign_pictures, text_spans)


def _format_paragraph(paragraph) -> None:
    fmt = paragraph.paragraph_format
    fmt.space_before = fmt.space_after = Pt(0)
    fmt.keep_with_next = False
    fmt.widow_control = False


def _add_runs(paragraph, spans) -> None:
    previous = None
    for span in spans:
        if previous is not None and span.bbox[0] - previous.bbox[2] > span.size * .3:
            if not previous.text.endswith(" ") and not span.text.startswith(" "):
                paragraph.add_run(" ")
        run = paragraph.add_run(span.text)
        run.font.name, run.font.size = font_name(span.font), Pt(span.size)
        run.bold, run.italic = bool(span.flags & 16), bool(span.flags & 2)
        run.font.color.rgb = RGBColor.from_string(f"{span.color:06X}")
        run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), font_name(span.font))
        previous = span


def _spacer(document, height: float) -> None:
    if height <= .5:
        return
    paragraph = document.add_paragraph()
    _format_paragraph(paragraph)
    paragraph.paragraph_format.line_spacing = Pt(height)
    paragraph.add_run().font.size = Pt(1)


def _cell_properties(cell, source: PdfCell, table: PdfTable) -> None:
    props = cell._tc.get_or_add_tcPr()
    shade = OxmlElement("w:shd")
    shade.set(qn("w:fill"), source.fill)
    props.insert_element_before(shade, "w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText", "w:vAlign")
    margins = OxmlElement("w:tcMar")
    for name, value in zip(("left", "right", "top", "bottom"), cell_margins(source)):
        edge = OxmlElement("w:" + name)
        edge.set(qn("w:w"), str(round(value * 20)))
        edge.set(qn("w:type"), "dxa")
        margins.append(edge)
    props.insert_element_before(margins, "w:textDirection", "w:tcFitText", "w:vAlign", "w:hideMark")
    borders = OxmlElement("w:tcBorders")
    for name in ("left", "right", "top", "bottom"):
        edge = OxmlElement("w:" + name)
        edge.set(qn("w:val"), "single")
        edge.set(qn("w:sz"), str(max(2, round(table.border_width * 8))))
        edge.set(qn("w:color"), table.border_color)
        borders.append(edge)
    props.insert_element_before(borders, "w:shd", "w:noWrap", "w:tcMar", "w:textDirection", "w:tcFitText", "w:vAlign")


def _add_table(document, source: PdfTable) -> None:
    check_cancelled()
    table = document.add_table(rows=source.rows, cols=source.cols)
    table.style = "Table Grid"
    table.autofit = False
    table.alignment = WD_TABLE_ALIGNMENT.LEFT
    widths = [right - left for left, right in zip(source.xs, source.xs[1:])]
    for col, width in zip(table.columns, widths):
        col.width = Pt(width)
    props = table._tbl.tblPr
    table_width = props.find(qn("w:tblW"))
    table_width.set(qn("w:w"), str(round(sum(widths) * 20)))
    table_width.set(qn("w:type"), "dxa")
    indent = OxmlElement("w:tblInd")
    indent.set(qn("w:w"), str(round(source.bbox[0] * 20)))
    indent.set(qn("w:type"), "dxa")
    props.insert_element_before(indent, "w:tblBorders", "w:shd", "w:tblLayout", "w:tblLook")
    for index, row in enumerate(table.rows):
        row.height = Pt(source.ys[index + 1] - source.ys[index])
        row.height_rule = WD_ROW_HEIGHT_RULE.EXACTLY
        cant_split = OxmlElement("w:cantSplit")
        row._tr.get_or_add_trPr().append(cant_split)
    for item in source.cells:
        check_cancelled()
        cell = table.cell(item.row, item.col)
        if item.rowspan > 1 or item.colspan > 1:
            cell = cell.merge(table.cell(item.row + item.rowspan - 1, item.col + item.colspan - 1))
        cell.width = Pt(item.bbox[2] - item.bbox[0])
        cell.text = ""
        _cell_properties(cell, item, source)
        cell_lines = lines(item.spans)
        for index, line in enumerate(cell_lines):
            paragraph = cell.paragraphs[0] if index == 0 else cell.add_paragraph()
            _format_paragraph(paragraph)
            paragraph.alignment = {"left": WD_ALIGN_PARAGRAPH.LEFT, "center": WD_ALIGN_PARAGRAPH.CENTER,
                                   "right": WD_ALIGN_PARAGRAPH.RIGHT}[cell_alignment(item)]
            paragraph.paragraph_format.line_spacing = Pt(max(span.size for span in line) * 1.15)
            _add_runs(paragraph, line)
        if not cell_lines:
            _format_paragraph(cell.paragraphs[0])
            cell.paragraphs[0].paragraph_format.line_spacing = Pt(1)
            cell.paragraphs[0].add_run().font.size = Pt(1)
        for picture in item.pictures:
            paragraph = cell.add_paragraph() if cell_lines else cell.paragraphs[0]
            _format_paragraph(paragraph)
            width, height = picture.bbox[2] - picture.bbox[0], picture.bbox[3] - picture.bbox[1]
            scale = min(1, max(1, item.bbox[2] - item.bbox[0] - 4) / width,
                        max(1, item.bbox[3] - item.bbox[1] - 4) / height)
            paragraph.paragraph_format.line_spacing = Pt(height * scale + 1)
            paragraph.add_run().add_picture(io.BytesIO(picture.png), width=Pt(width * scale), height=Pt(height * scale))


def convert_pdf_docx(source: Path, destination: Path, progress: Callable[[int | None], None],
                     warning: Callable[[str], None] = lambda _: None) -> None:
    from magic_converter.core.conversion.converter import ConversionError
    output = Document()
    output.styles["Normal"].font.name = "Malgun Gothic"
    output.styles["Normal"].paragraph_format.space_after = Pt(0)
    scanned = 0
    with PYMUPDF_LOCK, fitz.open(str(source)) as pdf:
        if pdf.needs_pass:
            raise ConversionError("암호로 보호된 PDF는 잠금을 해제한 뒤 변환하세요.")
        if not pdf.page_count:
            raise ConversionError("PDF에 페이지가 없습니다.")
        for page_index, page in enumerate(pdf):
            check_cancelled()
            if page.rotation:
                page.remove_rotation()
            width, height = page.rect.width, page.rect.height
            section = output.sections[0] if page_index == 0 else output.add_section(WD_SECTION_START.NEW_PAGE)
            section.page_width, section.page_height = Pt(width), Pt(height)
            section.left_margin = section.right_margin = section.top_margin = section.bottom_margin = Pt(0)
            section.header_distance = section.footer_distance = Pt(0)
            spans = text_spans(page)
            tables = extract_tables(page, spans)
            if not spans and not tables:
                # A scan has no cell/text structure. Preserve its full appearance
                # rather than presenting OCR-free pixels as an editable table.
                if page.get_image_info():
                    scanned += 1
                png = page.get_pixmap(dpi=200, alpha=False).tobytes("png")
                paragraph = output.add_paragraph()
                _format_paragraph(paragraph)
                paragraph.paragraph_format.line_spacing = Pt(height - 2)
                paragraph.add_run().add_picture(io.BytesIO(png), width=Pt(width - 2), height=Pt(height - 2))
                progress(min(99, (page_index + 1) * 100 // pdf.page_count))
                continue
            outside_images = assign_pictures(tables, pictures(page))
            outside_spans = [span for span in spans if not any(contains_center(table.bbox, span.bbox) for table in tables)
                             and not any(contains_center(image.bbox, span.bbox) for image in outside_images)]
            elements = [(table.bbox, "table", table) for table in tables]
            elements += [(image.bbox, "image", image) for image in outside_images]
            for line in lines(outside_spans):
                bounds = (min(s.bbox[0] for s in line), min(s.bbox[1] for s in line),
                          max(s.bbox[2] for s in line), max(s.bbox[3] for s in line))
                elements.append((bounds, "text", line))
            cursor = 0
            for bounds, kind, value in sorted(elements, key=lambda item: (item[0][1], item[0][0])):
                check_cancelled()
                gap = max(0, bounds[1] - cursor)
                if kind == "table":
                    _spacer(output, gap)
                    _add_table(output, value)
                else:
                    paragraph = output.add_paragraph()
                    _format_paragraph(paragraph)
                    fmt = paragraph.paragraph_format
                    fmt.space_before = Pt(gap)
                    fmt.left_indent = Pt(max(0, bounds[0]))
                    if kind == "image":
                        paragraph.add_run().add_picture(io.BytesIO(value.png), width=Pt(bounds[2] - bounds[0]),
                                                        height=Pt(bounds[3] - bounds[1]))
                    else:
                        fmt.line_spacing = Pt(max(span.size for span in value) * 1.15)
                        _add_runs(paragraph, value)
                emitted_height = ((bounds[3] - bounds[1]) if kind in {"table", "image"}
                                  else max(span.size for span in value) * 1.15)
                cursor = max(cursor, bounds[1]) + emitted_height
            progress(min(99, (page_index + 1) * 100 // pdf.page_count))
        output.save(destination)
    if scanned:
        warning(f"스캔 PDF {scanned}쪽은 이미지로 보존했습니다. 표·글자 편집에는 OCR 및 표 구조 인식이 필요합니다.")
    progress(100)
