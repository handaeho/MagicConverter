"""Workbook values interchange. Multi-sheet delimited output keeps all sheets."""

from __future__ import annotations

import csv
import datetime
import html
import io
import re
import zipfile
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree as ET

from lxml import etree
from openpyxl import Workbook, load_workbook
from openpyxl.formula.tokenizer import Token, Tokenizer, TokenizerError
from openpyxl.utils import get_column_letter

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.documents.document_converter import decode_text, html_to_pdf
from magic_converter.core.documents.spreadsheet_formula import describe_special_formula
from magic_converter.core.jobs.job_control import checked

ODS = "urn:oasis:names:tc:opendocument:xmlns:"
NS = {"office": ODS + "office:1.0", "table": ODS + "table:1.0", "text": ODS + "text:1.0"}
OPENFORMULA = ODS + "of:1.2"
LOCAL_REF = r"\$?[A-Za-z]{1,3}\$?[1-9][0-9]{0,6}"
ODS_REF = re.compile(rf"\[\.({LOCAL_REF})\]")
ROW_LIMIT = 1_048_576
COLUMN_LIMIT = 16_384
ODS_ROW_GROUPS = frozenset(f"{{{NS['table']}}}{group}"
                           for group in ("table-rows", "table-header-rows", "table-row-group"))


@dataclass(frozen=True)
class SheetFormula:
    original: str
    excel: str | None
    ods: str | None
    cached: object = None

    def __str__(self) -> str:
        return self.original


def _ods_arithmetic(formula: str) -> str | None:
    """Translate only local A1 arithmetic with shared Excel/OpenFormula semantics."""
    try:
        tokens = Tokenizer(formula).items
    except (TokenizerError, ValueError, IndexError):
        return None
    converted = []
    for token in tokens:
        if token.type == Token.OPERAND and token.subtype == Token.RANGE and re.fullmatch(LOCAL_REF, token.value):
            converted.append(f"[.{token.value}]")
        elif ((token.type, token.subtype) == (Token.OPERAND, Token.NUMBER)
              or token.type == Token.PAREN and token.value in {"(", ")"}
              or token.type in {Token.OP_IN, Token.OP_PRE} and token.value in {"+", "-", "*", "/"}):
            converted.append(token.value)
        else:
            return None
    return "of:=" + "".join(converted) if converted else None


def _xlsx_value(cell):
    value = cell.value
    if cell.data_type != "f":
        return value
    if isinstance(value, str):
        return SheetFormula(value, value, None)
    return SheetFormula(describe_special_formula(value), None, None)


def _ods_rows(table):
    for child in table:
        name = child.tag
        if name == f"{{{NS['table']}}}table-row":
            yield child
        elif name in ODS_ROW_GROUPS:
            yield from _ods_rows(child)


def _ods_value(cell):
    text = "\n".join("".join(p.itertext()) for p in cell.findall("text:p", NS))
    value_type = cell.get(f"{{{NS['office']}}}value-type")
    if value_type in {"float", "currency", "percentage"}:
        value = float(cell.get(f"{{{NS['office']}}}value", "0"))
    elif value_type == "boolean":
        value = cell.get(f"{{{NS['office']}}}boolean-value") == "true"
    else:
        value = (text or cell.get(f"{{{NS['office']}}}string-value")
                 or cell.get(f"{{{NS['office']}}}date-value") or cell.get(f"{{{NS['office']}}}time-value"))
        if value is None and value_type == "string":
            value = ""
    formula = cell.get(f"{{{NS['table']}}}formula")
    if formula is None:
        return value
    prefix, _, expression = formula.partition(":")
    if cell.nsmap.get(prefix) != OPENFORMULA or not expression.startswith("="):
        return SheetFormula(formula, None, None, value)
    excel = ODS_REF.sub(r"\1", expression)
    canonical = "of:" + expression
    return SheetFormula(formula, excel if _ods_arithmetic(excel) == canonical else None, canonical, value)


def _repeat_count(element, attribute):
    count = int(element.get(f"{{{NS['table']}}}number-{attribute}-repeated", "1"))
    if count < 1:
        raise ConversionError("ODS의 행·열 반복 수가 올바르지 않습니다.")
    return count


def _repeated_formula_as_text(value):
    # Expanding repeated ODF cells can change relative references. Preserve
    # the formula spelling until its repeated-cell semantics can be mapped.
    if isinstance(value, SheetFormula):
        return SheetFormula(value.original, None, None)
    return value


def read_sheets(source: Path) -> list[tuple[str, list[list]]]:
    ext = source.suffix.lower()
    if ext in {".csv", ".tsv"}:
        text = decode_text(source.read_bytes())
        return [(source.stem, list(checked(csv.reader(io.StringIO(text, newline=""), delimiter="\t" if ext == ".tsv" else ","))))]
    if ext == ".xlsx":
        workbook = load_workbook(source, read_only=True, data_only=False, keep_links=False)
        try:
            return [(sheet.title, [[_xlsx_value(cell) for cell in row] for row in checked(sheet.iter_rows())])
                    for sheet in checked(workbook)]
        finally:
            workbook.close()
    if ext == ".xls":
        import xlrd
        workbook = xlrd.open_workbook(source)
        try:
            result = []
            for sheet in checked(workbook.sheets()):
                rows = []
                for index in checked(range(sheet.nrows)):
                    row = []
                    for cell in sheet.row(index):
                        value = cell.value
                        if cell.ctype == xlrd.XL_CELL_DATE:
                            value = xlrd.xldate_as_datetime(value, workbook.datemode).isoformat()
                        elif cell.ctype == xlrd.XL_CELL_BOOLEAN:
                            value = bool(value)
                        row.append(value)
                    rows.append(row)
                result.append((sheet.name, rows))
            return result
        finally:
            workbook.release_resources()
    if ext == ".ods":
        with zipfile.ZipFile(source) as archive:
            root = etree.fromstring(archive.read("content.xml"),
                                    parser=etree.XMLParser(resolve_entities=False, no_network=True))
        if root.getroottree().docinfo.doctype:
            raise ConversionError("DTD가 포함된 ODS는 읽을 수 없습니다.")
        result = []
        for table in checked(root.findall("office:body/office:spreadsheet/table:table", NS)):
            rows = []
            blank_rows = 0
            for element in checked(_ods_rows(table)):
                cells = []
                blank_columns = 0
                for cell in element:
                    if cell.tag not in {f"{{{NS['table']}}}table-cell", f"{{{NS['table']}}}covered-table-cell"}:
                        continue
                    value = _ods_value(cell)
                    repeated = _repeat_count(cell, "columns")
                    if value is None:
                        blank_columns += repeated
                        continue
                    if len(cells) + blank_columns + repeated > COLUMN_LIMIT:
                        raise ConversionError("ODS 열 반복 수가 Excel 한도를 넘습니다.")
                    if repeated > 1:
                        value = _repeated_formula_as_text(value)
                    cells.extend([None] * blank_columns)
                    cells.extend([value] * repeated)
                    blank_columns = 0
                repeated = _repeat_count(element, "rows")
                if not cells:
                    blank_rows += repeated
                    continue
                if len(rows) + blank_rows + repeated > ROW_LIMIT:
                    raise ConversionError("ODS 행 반복 수가 Excel 한도를 넘습니다.")
                if repeated > 1:
                    cells = [_repeated_formula_as_text(cell) for cell in cells]
                rows.extend([] for _ in checked(range(blank_rows)))
                rows.extend(cells.copy() for _ in checked(range(repeated)))
                blank_rows = 0
            result.append((table.get(f"{{{NS['table']}}}name", "Sheet"), rows))
        return result
    raise ConversionError(f"지원하지 않는 표 입력: {ext}")


def write_sheets(sheets: list[tuple[str, list[list]]], destination: Path, suffix: str, notice=lambda _: None) -> None:
    unconverted_formulas = []

    def warn_formula(name, row, column):
        if len(unconverted_formulas) < 8:
            unconverted_formulas.append(f"{name}!{get_column_letter(column)}{row}")
        elif len(unconverted_formulas) == 8:
            unconverted_formulas.append("…")

    if suffix == ".xlsx":
        workbook = Workbook()
        workbook.remove(workbook.active)
        for name, rows in checked(sheets):
            safe_name = re.sub(r"[\\/*?:\[\]]", "_", name)[:31] or "Sheet"
            sheet = workbook.create_sheet(safe_name)
            for row_index, row in enumerate(checked(rows), 1):
                for column, value in enumerate(row, 1):
                    if value is None:
                        continue
                    if isinstance(value, SheetFormula):
                        if value.excel is not None:
                            sheet.cell(row_index, column, value.excel)
                            continue
                        warn_formula(name, row_index, column)
                        value = value.original
                    if isinstance(value, str) and len(value) > 32_767:
                        raise ConversionError(f"{name}!{get_column_letter(column)}{row_index} 텍스트가 Excel 셀 한도를 넘습니다.")
                    cell = sheet.cell(row_index, column, value)
                    if isinstance(value, str):
                        cell.data_type = "s"
        if not workbook.worksheets:
            workbook.create_sheet("Sheet")
        workbook.save(destination)
    elif suffix == ".ods":
        for prefix, uri in NS.items():
            ET.register_namespace(prefix, uri)
        root = ET.Element(f"{{{NS['office']}}}document-content", {f"{{{NS['office']}}}version": "1.2", "xmlns:of": "urn:oasis:names:tc:opendocument:xmlns:of:1.2"})
        spreadsheet = ET.SubElement(ET.SubElement(root, f"{{{NS['office']}}}body"), f"{{{NS['office']}}}spreadsheet")
        for name, rows in checked(sheets):
            table = ET.SubElement(spreadsheet, f"{{{NS['table']}}}table", {f"{{{NS['table']}}}name": name})
            for row_index, row in enumerate(checked(rows), 1):
                element = ET.SubElement(table, f"{{{NS['table']}}}table-row")
                for column, value in enumerate(row, 1):
                    attrs = {}
                    if isinstance(value, SheetFormula):
                        formula = value.ods
                        if formula is None and value.excel is not None:
                            formula = _ods_arithmetic(value.excel)
                        if formula is not None:
                            attrs[f"{{{NS['table']}}}formula"] = formula
                            value = value.cached
                        else:
                            warn_formula(name, row_index, column)
                            value = value.original
                    if isinstance(value, bool):
                        attrs.update({f"{{{NS['office']}}}value-type": "boolean",
                                      f"{{{NS['office']}}}boolean-value": str(value).lower()})
                    elif isinstance(value, (int, float)):
                        attrs.update({f"{{{NS['office']}}}value-type": "float", f"{{{NS['office']}}}value": str(value)})
                    elif isinstance(value, str):
                        attrs[f"{{{NS['office']}}}value-type"] = "string"
                    cell = ET.SubElement(element, f"{{{NS['table']}}}table-cell", attrs)
                    if value is not None:
                        ET.SubElement(cell, f"{{{NS['text']}}}p").text = str(value)
        manifest = '<manifest:manifest xmlns:manifest="urn:oasis:names:tc:opendocument:xmlns:manifest:1.0" manifest:version="1.2"><manifest:file-entry manifest:full-path="/" manifest:media-type="application/vnd.oasis.opendocument.spreadsheet"/><manifest:file-entry manifest:full-path="content.xml" manifest:media-type="text/xml"/></manifest:manifest>'
        with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet", compress_type=zipfile.ZIP_STORED)
            archive.writestr("content.xml", ET.tostring(root, encoding="utf-8", xml_declaration=True))
            archive.writestr("META-INF/manifest.xml", manifest)
    elif suffix in {".csv", ".tsv"}:
        with destination.open("w", encoding="utf-8-sig", newline="") as stream:
            writer = csv.writer(stream, delimiter="\t" if suffix == ".tsv" else ",")
            writer.writerows(checked(sheets[0][1] if sheets else []))
    elif suffix == ".pdf":
        parts = []
        for name, rows in checked(sheets):
            parts.append(f"<h2>{html.escape(name)}</h2><table>")
            for row in checked(rows):
                parts.append("<tr>" + "".join(f"<td>{html.escape('' if value is None else str(value))}</td>" for value in row) + "</tr>")
            parts.append("</table>")
        html_to_pdf("<html><body>" + "".join(parts) + "</body></html>", destination, landscape=True)
    else:
        raise ConversionError(f"지원하지 않는 표 출력: {suffix}")
    if unconverted_formulas:
        notice("수식 문법을 그대로 옮길 수 없어 원래 수식을 텍스트로 저장했습니다. " + ", ".join(unconverted_formulas))


def write_delimited_sheets(sheets: list[tuple[str, list[list]]], directory: Path, suffix: str) -> None:
    used: set[str] = set()
    for index, (name, rows) in enumerate(checked(sheets), start=1):
        safe = re.sub(r'[<>:"/\\|?*]', "_", name).rstrip(" .") or f"Sheet{index}"
        filename = f"{index:03d}_{safe}{suffix}"
        if filename.casefold() in used:
            filename = f"{index:03d}_Sheet{suffix}"
        used.add(filename.casefold())
        write_sheets([(name, rows)], directory / filename, suffix)
