"""Workbook values interchange. Multi-sheet delimited output keeps all sheets."""

from __future__ import annotations

import csv
import datetime
import html
import io
import re
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from openpyxl import Workbook, load_workbook

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.documents.document_converter import decode_text, html_to_pdf
from magic_converter.core.jobs.job_control import checked

ODS = "urn:oasis:names:tc:opendocument:xmlns:"
NS = {"office": ODS + "office:1.0", "table": ODS + "table:1.0", "text": ODS + "text:1.0"}


def read_sheets(source: Path) -> list[tuple[str, list[list]]]:
    ext = source.suffix.lower()
    if ext in {".csv", ".tsv"}:
        text = decode_text(source.read_bytes())
        return [(source.stem, list(checked(csv.reader(io.StringIO(text, newline=""), delimiter="\t" if ext == ".tsv" else ","))))]
    if ext == ".xlsx":
        workbook = load_workbook(source, read_only=True, data_only=False, keep_links=False)
        try:
            return [(sheet.title, [list(row) for row in checked(sheet.iter_rows(values_only=True))]) for sheet in checked(workbook)]
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
            root = ET.fromstring(archive.read("content.xml"))
        result = []
        for table in checked(root.findall(".//table:table", NS)):
            rows = []
            for element in checked(table.findall("table:table-row", NS)):
                cells = []
                for cell in element:
                    if cell.tag not in {f"{{{NS['table']}}}table-cell", f"{{{NS['table']}}}covered-table-cell"}:
                        continue
                    text = "\n".join("".join(p.itertext()) for p in cell.findall("text:p", NS))
                    value_type = cell.get(f"{{{NS['office']}}}value-type", "string")
                    if value_type in {"float", "currency", "percentage"}:
                        value = float(cell.get(f"{{{NS['office']}}}value", "0"))
                    elif value_type == "boolean":
                        value = cell.get(f"{{{NS['office']}}}boolean-value") == "true"
                    else:
                        value = text or cell.get(f"{{{NS['office']}}}date-value", "")
                    repeated = int(cell.get(f"{{{NS['table']}}}number-columns-repeated", "1"))
                    if repeated > 16384:
                        if value == "":
                            continue
                        raise ConversionError("ODS 열 반복 수가 Excel 한도를 넘습니다.")
                    cells.extend([value] * repeated)
                while cells and cells[-1] == "":
                    cells.pop()
                repeated = int(element.get(f"{{{NS['table']}}}number-rows-repeated", "1"))
                if not cells:
                    if repeated == 1:
                        rows.append([])
                    continue
                if len(rows) + repeated > 1_048_576:
                    raise ConversionError("ODS 행 반복 수가 Excel 한도를 넘습니다.")
                rows.extend(cells.copy() for _ in checked(range(repeated)))
            while rows and not rows[-1]:
                rows.pop()
            result.append((table.get(f"{{{NS['table']}}}name", "Sheet"), rows))
        return result
    raise ConversionError(f"지원하지 않는 표 입력: {ext}")


def write_sheets(sheets: list[tuple[str, list[list]]], destination: Path, suffix: str) -> None:
    if suffix == ".xlsx":
        workbook = Workbook()
        workbook.remove(workbook.active)
        for name, rows in checked(sheets):
            safe_name = re.sub(r"[\\/*?:\[\]]", "_", name)[:31] or "Sheet"
            sheet = workbook.create_sheet(safe_name)
            for row in checked(rows):
                sheet.append(row)
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
            for row in checked(rows):
                element = ET.SubElement(table, f"{{{NS['table']}}}table-row")
                for value in row:
                    attrs = {f"{{{NS['office']}}}value-type": "string"}
                    if isinstance(value, bool):
                        attrs = {f"{{{NS['office']}}}value-type": "boolean", f"{{{NS['office']}}}boolean-value": str(value).lower()}
                    elif isinstance(value, (int, float)):
                        attrs = {f"{{{NS['office']}}}value-type": "float", f"{{{NS['office']}}}value": str(value)}
                    elif isinstance(value, str) and value.startswith("="):
                        attrs[f"{{{NS['table']}}}formula"] = "of:" + value
                    cell = ET.SubElement(element, f"{{{NS['table']}}}table-cell", attrs)
                    ET.SubElement(cell, f"{{{NS['text']}}}p").text = "" if value is None else str(value)
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


def write_delimited_sheets(sheets: list[tuple[str, list[list]]], directory: Path, suffix: str) -> None:
    used: set[str] = set()
    for index, (name, rows) in enumerate(checked(sheets), start=1):
        safe = re.sub(r'[<>:"/\\|?*]', "_", name).rstrip(" .") or f"Sheet{index}"
        filename = f"{index:03d}_{safe}{suffix}"
        if filename.casefold() in used:
            filename = f"{index:03d}_Sheet{suffix}"
        used.add(filename.casefold())
        write_sheets([(name, rows)], directory / filename, suffix)
