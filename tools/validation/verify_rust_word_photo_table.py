"""Exercise editable DOCX -> HWPX pagination and fixed-height cell text."""

import argparse
from io import BytesIO
from pathlib import Path
import subprocess
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from docx import Document
from docx.enum.style import WD_STYLE_TYPE
from docx.enum.table import WD_CELL_VERTICAL_ALIGNMENT
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from PIL import Image
import pymupdf


def element(name, **attributes):
    node = OxmlElement(f"w:{name}")
    for name, value in attributes.items():
        node.set(qn(f"w:{name}"), value)
    return node


def photo_table(path):
    doc = Document()
    page = doc.sections[0]
    page.page_width, page.page_height = Pt(595), Pt(842)
    page.left_margin = page.right_margin = page.top_margin = page.bottom_margin = Pt(45)
    normal = doc.styles["Normal"]
    normal.font.size = Pt(11)
    normal.paragraph_format.space_after = Pt(0)
    normal.paragraph_format.line_spacing = 1
    style = doc.styles.add_style("PhotoTable", WD_STYLE_TYPE.TABLE)
    style.base_style = doc.styles["Table Grid"]
    first_row = element("tblStylePr", type="firstRow")
    props = element("tcPr")
    props.append(element("vAlign", val="center"))
    first_row.append(props)
    style._element.append(first_row)

    doc.add_paragraph("PHOTO REPORT")
    table = doc.add_table(rows=13, cols=4)
    table.style = style
    table.autofit = False
    for col in table.columns:
        col.width = Pt(126)
    table.rows[0]._tr.get_or_add_trPr().append(element("tblHeader"))
    table.rows[0].height = Pt(28)
    table.cell(0, 0).merge(table.cell(0, 3)).text = "REPEATING HEADER"
    for index in range(6):
        labels, pictures = table.rows[1 + 2 * index : 3 + 2 * index]
        labels.height = Pt(24)
        for column, alignment in enumerate(("FIXED", "TOP", "CENTER", "BOTTOM")):
            cell = labels.cells[column]
            cell.text = f"{alignment}{index}"
            if alignment == "FIXED":
                cell.paragraphs[0].paragraph_format.line_spacing = Pt(22)
            else:
                cell.vertical_alignment = getattr(WD_CELL_VERTICAL_ALIGNMENT, alignment)
        pictures.height = Pt(145)
        for column in (0, 2):
            cell = pictures.cells[column].merge(pictures.cells[column + 1])
            png = BytesIO()
            Image.new("RGB", (250, 130), (23 + 16 * index, 42 + 23 * column, 90)).save(png, "PNG")
            cell.paragraphs[0].add_run().add_picture(png, width=Pt(235))
    doc.add_paragraph("AFTER TABLE")
    doc.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("candidate", "rhwp", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    source = root / "photo-table.docx"
    photo_table(source)
    subprocess.run([
        args.candidate.resolve(), "convert", "--data-dir", root / "profile",
        "--to", "hwpx", source, "--output", root,
    ], check=True, capture_output=True, timeout=30)
    hwpx = root / "photo-table_converted.hwpx"
    with ZipFile(hwpx) as archive:
        section = ET.fromstring(archive.read("Contents/section0.xml"))
        table = section.find(".//{*}tbl")
        assert table.get("rowCnt") == "13"
        assert table.find("{*}tr/{*}tc/{*}cellSpan").get("colSpan") == "4"
    pdf = root / "photo-table.pdf"
    result = subprocess.run([args.rhwp.resolve(), "export-pdf", hwpx, "-o", pdf],
                            check=True, capture_output=True, timeout=30)
    assert b"LAYOUT_OVERFLOW" not in result.stderr
    with pymupdf.open(pdf) as report:
        assert len(report) == 2
        assert "PHOTO REPORT" in report[0].get_text()
        assert "AFTER TABLE" in report[-1].get_text()
        assert all("REPEATING HEADER" in page.get_text() for page in report)
        assert sum(len(page.get_image_info()) for page in report) == 12
        for page in report:
            assert all(44 <= image["bbox"][1] < image["bbox"][3] <= 798
                       for image in page.get_image_info())
        y = {word[4]: (word[1] + word[3]) / 2 for word in report[0].get_text("words")}
        assert y["TOP0"] + 3 < y["FIXED0"] < y["BOTTOM0"] - 1, y
        assert abs(y["FIXED0"] - y["CENTER0"]) < 2, y
    print("Photo table: two pages, 12 pictures, repeated header and aligned labels.")


if __name__ == "__main__":
    main()
