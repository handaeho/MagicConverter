"""Compare shared Office and HWP fixtures, including editable PDF output."""

import argparse
from hashlib import sha256
import io
import json
import os
from pathlib import Path
import subprocess
import sys
from time import perf_counter
from zipfile import ZipFile

from docx import Document
from docx.oxml import parse_xml
from docx.shared import Inches, Pt, RGBColor
from openpyxl import Workbook, load_workbook
from openpyxl.drawing.image import Image as SheetImage
from openpyxl.styles import Font, PatternFill
from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
import pymupdf as fitz

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run
from tools.validation.verify_rust_office_render_parity import compare_pdf

OFFICE = {
    "docx": ("word", "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
             {"docm": "application/vnd.ms-word.document.macroEnabled.main+xml",
              "dotx": "application/vnd.openxmlformats-officedocument.wordprocessingml.template.main+xml",
              "dotm": "application/vnd.ms-word.template.macroEnabledTemplate.main+xml"}),
    "xlsx": ("xl", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml",
             {"xlsm": "application/vnd.ms-excel.sheet.macroEnabled.main+xml",
              "xltx": "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml",
              "xltm": "application/vnd.ms-excel.template.macroEnabled.main+xml"}),
    "pptx": ("ppt", "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
             {"pptm": "application/vnd.ms-powerpoint.presentation.macroEnabled.main+xml",
              "potx": "application/vnd.openxmlformats-officedocument.presentationml.template.main+xml",
              "potm": "application/vnd.ms-powerpoint.template.macroEnabled.main+xml",
              "ppsx": "application/vnd.openxmlformats-officedocument.presentationml.slideshow.main+xml",
              "ppsm": "application/vnd.ms-powerpoint.slideshow.macroEnabled.main+xml"}),
}


def make_office_fixtures(folder):
    picture = folder / "picture.png"
    image = Image.new("RGB", (40, 30), (245, 140, 25))
    image.paste((20, 40, 190), (0, 10, 20, 18))
    image.save(picture)
    word = Document()
    section = word.sections[0]
    section.header.paragraphs[0].text = "공통 머리말"
    section.footer.paragraphs[0].text = "공통 꼬리말"
    section.left_margin = Pt(50)
    p = word.add_paragraph()
    p.paragraph_format.left_indent = Pt(12)
    p.paragraph_format.space_after = Pt(8)
    span = p.add_run("한글과 Latin")
    span.bold, span.italic, span.font.name, span.font.size = True, True, "Malgun Gothic", Pt(13)
    span.font.color.rgb = RGBColor(22, 55, 99)
    table = word.add_table(rows=3, cols=2)
    table.cell(0, 0).merge(table.cell(0, 1)).text = "가로 병합"
    table.cell(1, 0).merge(table.cell(2, 0)).text = "세로 병합"
    table.cell(1, 1).text = "한글 표"
    table.cell(2, 1).text = "둘째 줄"
    word.add_picture(str(picture), width=Inches(0.5))
    word.add_paragraph("마지막 본문")
    word.save(folder / "rich.docx")
    book = Workbook()
    sheet = book.active
    sheet.title = "합성 한글"
    sheet.merge_cells("A1:B1")
    sheet["A1"] = "병합 제목"
    sheet["A1"].font = Font(name="Calibri", size=12, bold=True, italic=True)
    sheet["A1"].fill = PatternFill("solid", fgColor="EFABCD")
    sheet["A2"], sheet["B2"] = "=SUM(1,2)", "둘째 열"
    sheet.add_image(SheetImage(picture), "D3")
    book.save(folder / "rich.xlsx")
    deck = Presentation()
    slide = deck.slides.add_slide(deck.slide_layouts[6])
    slide.shapes.add_textbox(Inches(1), Inches(1), Inches(3), Inches(1)).text = "편집 슬라이드\nsecond line"
    table = slide.shapes.add_table(3, 2, Inches(1), Inches(2), Inches(4), Inches(2)).table
    table.cell(0, 0).merge(table.cell(0, 1))
    table.cell(0, 0).text = "병합 제목"
    table.cell(1, 0).text = "행 2"
    table.cell(2, 1).text = "행 3"
    slide.shapes.add_picture(str(picture), Inches(6), Inches(2), width=Inches(1))
    deck.save(folder / "rich.pptx")
    for extension, (prefix, source_type, variants) in OFFICE.items():
        source = folder / f"rich.{extension}"
        yield source
        for suffix, variant_type in variants.items():
            path = folder / f"rich.{suffix}"
            with ZipFile(source) as original, ZipFile(path, "w") as modified:
                for name in original.namelist():
                    data = original.read(name)
                    if name == "[Content_Types].xml":
                        data = data.replace(source_type.encode(), variant_type.encode())
                    modified.writestr(name, data)
                if suffix.endswith("m"):
                    modified.writestr(f"{prefix}/vbaProject.bin", b"synthetic test data")
            yield path


def add_stream(document, page, commands):
    stream = document.get_new_xref()
    document.update_object(stream, "<<>>")
    document.update_stream(stream, commands)
    page.set_contents(stream)


def grouped_word(source, picture):
    doc = Document()
    run = doc.add_paragraph("Editable grouped objects").add_run()
    pic = run.add_picture(str(picture), width=Inches(.5))
    inline = pic._inline
    drawing = inline.find("{*}graphic").find("{*}graphicData")
    image = drawing[0]
    image_props = image.find("{*}spPr")
    offset = image_props.find("{*}xfrm").find("{*}off")
    offset.set("x", "990600")
    offset.set("y", "127000")
    a = "http://schemas.openxmlformats.org/drawingml/2006/main"
    wpg = "http://schemas.microsoft.com/office/word/2010/wordprocessingGroup"
    wps = "http://schemas.microsoft.com/office/word/2010/wordprocessingShape"

    def transform(x, y, w, h):
        return f'<a:off x="{x}" y="{y}"/><a:ext cx="{w}" cy="{h}"/>'

    def group_transform(x, y, w, h):
        return (f'<wpg:grpSpPr><a:xfrm>{transform(x, y, w, h)}'
                f'<a:chOff x="0" y="0"/><a:chExt cx="{w}" cy="{h}"/></a:xfrm></wpg:grpSpPr>')

    def shape(kind, x, y, w, h, color):
        return (f'<wps:wsp><wps:cNvSpPr/><wps:spPr><a:xfrm>{transform(x, y, w, h)}</a:xfrm>'
                f'<a:prstGeom prst="{kind}"><a:avLst/></a:prstGeom>'
                f'<a:solidFill><a:srgbClr val="{color}"/></a:solidFill>'
                f'<a:ln w="12700"><a:solidFill><a:srgbClr val="333333"/></a:solidFill></a:ln>'
                f'</wps:spPr><wps:bodyPr/></wps:wsp>')

    group = parse_xml(
        f'<wpg:wgp xmlns:a="{a}" xmlns:wpg="{wpg}" xmlns:wps="{wps}"><wpg:cNvGrpSpPr/>'
        f'{group_transform(0, 0, 1524000, 762000)}'
        f'{shape("rect", 63500, 63500, 381000, 381000, "EE2222")}'
        f'<wpg:grpSp><wpg:cNvPr id="20" name="Nested"/><wpg:cNvGrpSpPr/>'
        f'{group_transform(508000, 127000, 444500, 508000)}'
        f'{shape("ellipse", 0, 0, 381000, 381000, "22AA55")}</wpg:grpSp></wpg:wgp>'
    )
    group.append(image)
    drawing.append(group)
    drawing.set("uri", wpg)
    inline.extent.cx, inline.extent.cy = 1524000, 762000
    doc.save(source)


def verify_group(source, output):
    from lxml import etree
    with ZipFile(output) as archive:
        xml = etree.fromstring(archive.read("Contents/section0.xml"))
        roots = xml.xpath(".//*[local-name()='container' and @groupLevel='0']")
        root, = roots
        kinds = lambda item: [etree.QName(child).localname for child in item if
                              etree.QName(child).localname in ("rect", "ellipse", "container", "pic")]
        assert kinds(root) == ["rect", "container", "pic"]
        nested, = root.xpath("./*[local-name()='container' and @groupLevel='1']")
        assert kinds(nested) == ["ellipse"]
        ellipse, = nested.xpath("./*[local-name()='ellipse' and @groupLevel='2']")
        nested_offset, = ellipse.xpath("./*[local-name()='offset']")
        assert (nested_offset.get("x"), nested_offset.get("y")) == ("4000", "1000")
        offsets = [tuple(map(int, (el.get("x"), el.get("y")))) for el in root.xpath(
            "./*[@groupLevel]/*[local-name()='offset']")]
        assert offsets == [(500, 500), (4000, 1000), (7800, 1000)], offsets
        picture, = root.xpath("./*[local-name()='pic']")
        dimensions, = picture.xpath("./*[local-name()='orgSz']")
        assert (dimensions.get("width"), dimensions.get("height")) == ("3600", "2700")
        image, = [name for name in archive.namelist() if name.startswith("BinData/")]
        with Image.open(io.BytesIO(archive.read(image))) as stored, Image.open(source) as original:
            assert stored.convert("RGBA").tobytes() == original.convert("RGBA").tobytes()
        return {"members": kinds(root), "nested": kinds(nested), "offsets_hwpunits": offsets}

def pdf_fixtures(folder):
    path = folder / "editable.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=360, height=280)
        page.insert_text((20, 30), "Selectable heading", fontsize=12)
        for x in (20, 100, 180):
            page.draw_line((x, 70 if x != 100 else 100), (x, 160))
        for y in (70, 100, 130, 160):
            page.draw_line((20, y), (180, y))
        for x, y, text in ((25, 90, "Merged heading"), (25, 118, "A1"), (105, 118, "B1"), (25, 148, "A2"), (105, 148, "B2")):
            page.insert_text((x, y), text, fontsize=11)
        page.draw_rect((20, 180, 130, 230), color=(.1, .2, .4), fill=(.1, .4, .7))
        page.insert_text((28, 210), "Editable box", fontsize=12, color=(1, 1, 1))
        page.draw_rect((95, 200, 125, 219), color=None, fill=(1, .6, .1))
        page.draw_circle((185, 215), 20, color=(.2, .4, .6), fill=(.5, .8, .9))
        page.draw_bezier((210, 220), (215, 170), (265, 270), (290, 230), color=(.2, .4, .5), width=2)
        page.insert_image((260, 30, 300, 60), filename=folder / "picture.png")
        page = pdf.new_page(width=240, height=180)
        page.insert_text((20, 30), "Second page")
        pdf.save(path)
    yield path
    path = folder / "clipping.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=240, height=180)
        add_stream(pdf, page, b"q 60 20 100 100 re W n 60 70 m 60 97.6 82.4 120 110 120 c 137.6 120 160 97.6 160 70 c 160 42.4 137.6 20 110 20 c 82.4 20 60 42.4 60 70 c W n 0.2 0.7 0.5 rg 60 20 100 100 re f Q")
        pdf.save(path)
    yield path
    path = folder / "soft-mask.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=240, height=180)
        mask = pdf.get_new_xref()
        pdf.update_object(mask, "<< /Type /XObject /Subtype /Form /BBox [0 0 240 180] /Group << /S /Transparency /CS /DeviceGray >> /Resources <<>> >>")
        pdf.update_stream(mask, b"0.5 g 20 30 100 80 re f")
        pdf.xref_set_key(page.xref, "Resources", f"<< /ExtGState << /SM << /SMask << /S /Luminosity /G {mask} 0 R >> >> >> >>")
        add_stream(pdf, page, b"q /SM gs 1 0 0 rg 0 0 240 180 re f Q")
        pdf.save(path)
    yield path
    path = folder / "pattern.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=240, height=180)
        pattern = pdf.get_new_xref()
        pdf.update_object(pattern, "<< /Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 /BBox [0 0 10 10] /XStep 10 /YStep 10 /Resources <<>> >>")
        pdf.update_stream(pattern, b"1 0 0 rg 0 0 5 5 re f")
        pdf.xref_set_key(page.xref, "Resources", f"<< /Pattern << /P {pattern} 0 R >> >>")
        add_stream(pdf, page, b"/Pattern cs /P scn 20 30 160 100 re f")
        pdf.save(path)
    yield path
    path = folder / "scan.pdf"
    with fitz.open() as pdf:
        page = pdf.new_page(width=240, height=180)
        page.insert_image(page.rect, filename=folder / "picture.png")
        pdf.save(path)
    yield path


def slide_model(path, edited):
    deck = Presentation(path)
    slides = []
    for slide in deck.slides:
        shapes = []
        for shape in slide.shapes:
            item = {"kind": str(shape.shape_type), "bounds": [round(getattr(shape, field).pt, 2) for field in ("left", "top", "width", "height")]}
            if shape.has_text_frame:
                item["text"] = shape.text
            if shape.has_table:
                item["cells"] = [[cell.text for cell in row.cells] for row in shape.table.rows]
            if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
                with Image.open(io.BytesIO(shape.image.blob)) as image:
                    rgba = image.convert("RGBA")
                    item["alpha"] = rgba.getchannel("A").getextrema()
                    item["pixel_sha256"] = sha256(rgba.tobytes()).hexdigest()
            shapes.append(item)
        slides.append(shapes)
    shape = next((item for slide in deck.slides for item in slide.shapes if item.has_table), None)
    if shape is not None:
        shape.table.cell(0, 0).text = "Edited table cell"
        deck.save(edited)
        assert any(s.has_table and s.table.cell(0, 0).text == "Edited table cell" for s in Presentation(edited).slides[0].shapes)
    return slides


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--rhwp", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--render", action="store_true", help="Render selected output pairs in the same Office or rhwp engine.")
    args = parser.parse_args()
    root, baseline, worker = args.output.resolve(), args.baseline.resolve(), args.worker.resolve()
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    sys.path.insert(0, str(baseline))
    from magic_converter.core.documents.hwp_documents import read_hwpx, read_docx
    from magic_converter.core.documents.document_audit import inventory, compare
    from magic_converter.core.engines.hwp_converter import validate_output

    os.environ["MAGIC_CONVERTER_RHWP"] = str(args.rhwp.resolve())
    hwpx = ROOT / "tests/fixtures/hwp/table-picture-spacing.hwpx"
    hwp = inputs / "rich.hwp"
    subprocess.run([args.rhwp.resolve(), "convert", hwpx, hwp], check=True, capture_output=True, timeout=30)
    office = list(make_office_fixtures(inputs))
    from magic_converter.core.documents.hwp_documents import InlineLine
    from magic_converter.core.documents.inline_drawing import write_word_line
    line_source = inputs / "inline-line.docx"
    line_doc = Document()
    run_with_line = line_doc.add_paragraph("Horizontal line ").add_run()
    write_word_line(run_with_line, InlineLine(100, 20, 5, 90, 8, 1.5, "2A6090"), 300, 1000)
    line_doc.save(line_source)
    group_source = inputs / "grouped.docx"
    grouped_word(group_source, inputs / "picture.png")
    pdfs = list(pdf_fixtures(inputs))
    cases = [(item, f"document:{ext}") for item in office for ext in ("hwp", "hwpx")]
    cases.append((line_source, "document:hwpx"))
    cases.append((group_source, "document:hwpx"))
    cases += [(item, f"document:{ext}") for item in (hwp, hwpx) for ext in ("docx", "md", "html")]
    cases += [(pdfs[0], action) for action in ("document:docx", "document:xlsx", "pptx_editable")]
    cases += [(item, "pptx_editable") for item in pdfs[1:]]
    cases += [(pdfs[-1], "document:docx"), (pdfs[-1], "document:xlsx")]
    evidence = {"baseline": str(baseline), "worker_sha256": digest(worker), "cases": {}}

    def model(path, source, action, backend):
        if path.suffix == ".pptx":
            return slide_model(path, path.parent.parent / f"edited-{backend}.pptx")
        if path.suffix == ".xlsx":
            book = load_workbook(path)
            try:
                return {sheet.title: [list(row) for row in sheet.values] for sheet in book}
            finally:
                book.close()
        if path.suffix in {".hwp", ".hwpx", ".docx"}:
            validate_output(path)
            if path.suffix == ".hwp":
                temp = path.with_suffix(".audit.hwpx")
                subprocess.run([args.rhwp.resolve(), "export-hwpx", path, temp], check=True, capture_output=True, timeout=30)
                return read_hwpx(temp)
            if path.suffix == ".hwpx":
                return read_hwpx(path)
            if source.suffix == ".pdf":
                doc = Document(path)
                result = {"text": [p.text for p in doc.paragraphs if p.text],
                          "tables": [[[cell.text for cell in row.cells] for row in table.rows] for table in doc.tables],
                          "images": len(doc.element.xpath(".//wp:inline | .//wp:anchor")),
                          "sections": len(doc.sections)}
                if doc.tables:
                    doc.tables[0].cell(0, 0).text = "Edited table cell"
                    edited = path.parent.parent / f"edited-{backend}.docx"
                    doc.save(edited)
                    assert Document(edited).tables[0].cell(0, 0).text == "Edited table cell"
                return result
            return read_docx(path)
        if path.suffix == ".html":
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
            return {"text": " ".join(soup.stripped_strings), "tables": len(soup.find_all("table")), "images": len(soup.find_all("img"))}
        return path.read_text(encoding="utf-8")

    def save():
        rows = evidence["cases"].values()
        evidence["total"] = len(rows)
        evidence["same_state"] = sum(row.get("same_state", False) for row in rows)
        evidence["same_content"] = sum(row.get("same_content", False) for row in rows)
        (root / "report.json").write_text(json.dumps(evidence, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    for source, action in cases:
        key = f"{source.name}-{action.replace(':', '-')}"
        print(key, flush=True)
        row = evidence["cases"][key] = {"source_sha256": digest(source), "action": action}
        results = []
        outputs = []
        for backend, command, environment in (
            ("python", [sys.executable, "-B", baseline / "run_magic_converter.py"], None),
            ("rust", [worker], native_environment()),
        ):
            folder = root / key / backend
            folder.mkdir(parents=True)
            start = perf_counter()
            code, receipt = run(command, ("convert", "--action", action, "--timeout", 90, "--output", folder,
                                          "--data-dir", root / f"profile-{backend}", source), environment=environment)
            row[backend] = {"code": code, "seconds": perf_counter() - start, "receipt": receipt}
            if code == 0:
                output = Path(receipt["jobs"][0]["outputs"][0])
                outputs.append(output)
                row[backend]["output_sha256"] = digest(output)
                try:
                    result = model(output, source, action, backend)
                    row[backend]["model"] = ({"text": result.text(), "objects": inventory(result)}
                                             if callable(getattr(result, "text", None)) else result)
                    results.append(result)
                except Exception as error:
                    row[backend]["inspection_error"] = repr(error)
        row["same_state"] = row["python"]["code"] == row["rust"]["code"]
        row["same_content"] = row["same_state"] and (row["python"]["code"] != 0 or
            (len(results) == 2 and row["python"]["model"] == row["rust"]["model"]))
        if source.name == "editable.pdf" and action == "pptx_editable" and len(outputs) == 2:
            from lxml import etree

            slide = Presentation(outputs[1]).slides[0]
            table, = [shape.table for shape in slide.shapes if shape.has_table]
            assert table.cell(0, 0).is_merge_origin
            assert not any(shape.shape_type == MSO_SHAPE_TYPE.LINE for shape in slide.shapes)
            properties = table.cell(0, 0)._tc.get_or_add_tcPr()
            edges = [etree.QName(node).localname for node in properties
                     if any(etree.QName(child).localname == "solidFill" for child in node)]
            assert {"lnL", "lnT", "lnB"} <= set(edges)
            row["rust_native_table_edges"] = edges
        if source == line_source:
            assert row["same_content"] and len(row["rust"]["model"]["objects"]["lines"]) == 1
        if source == group_source:
            assert row["rust"]["code"] == 0
            rust_group = Path(row["rust"]["receipt"]["jobs"][0]["outputs"][0])
            row["rust_native_group"] = verify_group(inputs / "picture.png", rust_group)
        if len(results) == 2 and callable(getattr(results[0], "text", None)):
            row["audit"] = compare(*results)
        sample = ((source.suffix in (".docx", ".pptx", ".xlsx") and action == "document:hwpx") or
                  (source.suffix == ".hwpx" and action == "document:docx") or
                  (source.suffix == ".pdf" and action in ("document:docx", "pptx_editable")))
        if args.render and sample and len(outputs) == 2:
            try:
                rendered = []
                for document in outputs:
                    pdf = document.parent / "render-audit.pdf"
                    if document.suffix == ".hwpx":
                        command = [args.rhwp.resolve(), "export-pdf", document, "-o", pdf]
                    else:
                        kind = "word" if document.suffix == ".docx" else "powerpoint"
                        command = [worker, "office-worker", kind, document, pdf]
                    process = subprocess.run(command, capture_output=True, timeout=90, env=native_environment())
                    if process.returncode:
                        raise RuntimeError(process.stderr.decode("utf-8", errors="replace"))
                    rendered.append(pdf)
                row["render"] = compare_pdf(*rendered)
                if source.suffix == ".pdf":
                    row["source_render"] = {
                        backend: compare_pdf(source, output)
                        for backend, output in zip(("python", "rust"), rendered, strict=True)
                    }
                    if source == pdfs[-1] and action == "document:docx":
                        import pymupdf

                        with pymupdf.open(source) as original, pymupdf.open(rendered[1]) as converted:
                            expected = original[0].get_image_info()[0]["bbox"]
                            actual = converted[0].get_image_info()[0]["bbox"]
                            row["scan_image_bbox"] = {"source": expected, "rust": actual}
                            assert len(converted) == len(original)
                            assert max(abs(a - b) for a, b in zip(expected, actual, strict=True)) <= 0.1
            except Exception as error:
                row["render_error"] = repr(error)
        save()


if __name__ == "__main__":
    main()
