"""Check chart workbooks, PDF paint regions and Word drawing groups."""

import argparse
from copy import copy, deepcopy
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import runpy
import subprocess
import sys
import time
from zipfile import ZipFile

import pymupdf as fitz
from lxml import etree
from openpyxl import load_workbook
from PIL import Image, ImageDraw
from pptx import Presentation
from pptx.chart.data import CategoryChartData
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE_TYPE

from tools.validation.verify_rust_editable_office_parity import add_stream, grouped_word, pdf_fixtures
from tools.validation.verify_rust_full_parity import digest, native_environment, run
from tools.validation.verify_rust_office_render_parity import compare_pdf


def chart_sources(folder):
    fixtures = runpy.run_path("tests/pdf/test_pdf_charts.py")
    columns = fixtures["column_fixture"]
    label = fixtures["label"]

    def line_series(page):
        for value in range(0, 21, 5):
            label(page, 55, 280-value*10, str(value))
        for i, name in enumerate(("May", "Jun", "Jul", "Aug")):
            label(page, 80+i*55, 293, name)
        for values, color in (((4, 9, 7, 16), fixtures["BLUE"]), ((2, 5, 13, 11), fixtures["ORANGE"])):
            points = [fitz.Point(80+i*55, 280-v*10) for i, v in enumerate(values)]
            page.draw_polyline(points, color=color, width=1.5)
            for point, value in zip(points, values, strict=True):
                label(page, point.x, point.y-10, str(value))
        columns(page, values=(3, 8, 6, 14), offset=330, categories=["P", "Q", "R", "S"])

    def horizontal(page):
        for value in (0, 20, 40, 60, 80, 100):
            label(page, 130+value*3, 275, str(value))
        for i, (name, value) in enumerate(zip(("West", "East", "Central", "Coast"), (25, 60, 45, 80), strict=True)):
            y = 100+i*40
            page.draw_rect((130, y-10, 130+value*3, y+10), color=None, fill=fixtures["BLUE"])
            label(page, 95, y, name)
            label(page, 130+value*3+12, y, str(value))

    def logarithmic(page):
        columns(page)
        ticks = (280, 230, 180, 130, 80)
        for y in ticks:
            page.add_redact_annot((40, y-9, 72, y+9), fill=(1, 1, 1))
        page.apply_redactions()
        for y, value in zip(ticks, (1, 10, 100, 1000, 10000), strict=True):
            label(page, 60, y, str(value))

    def shared_text(page):
        columns(page, missing=(0,))
        shared = b"\nq BT /helv 9 Tf 1 0 0 1 105 206.5 Tm (4) Tj 1 0 0 1 400 50 Tm (Outside caption) Tj ET Q"
        add_stream(page.parent, page, page.read_contents() + shared)

    cases = [
        ("columns", columns, 1),
        ("estimated", lambda page: columns(page, missing=(0, 2)), 1),
        ("grouped-percent", lambda page: columns(page, values=(.2,.4,.3,.7), grouped=(.3,.2,.5,.6), high=1, percentage=True), 1),
        ("negative", lambda page: columns(page, values=(-4,8,-7,16), low=-10, high=20), 1),
        ("axis-range", lambda page: columns(page, values=(2,4,7,6), high=100), 1),
        ("quadrilaterals", lambda page: columns(page, quadrilaterals=True), 1),
        ("horizontal", horizontal, 1),
        ("combo", lambda page: columns(page, combo=True), 1),
        ("multiple", line_series, 2),
        ("stacked", lambda page: columns(page, stacked=True), 0),
        ("logarithmic", logarithmic, 0),
        ("shared-path", lambda page: columns(page, outside_shape=True), 0),
        ("shared-text", shared_text, 0),
        ("batched-markers", lambda page: columns(page, combo=True, batched_markers=True), 0),
    ]
    for name, create, charts in cases:
        source = folder / f"{name}.pdf"
        with fitz.open() as doc:
            create(doc.new_page(width=700, height=440))
            doc.save(source)
        yield source, charts


def inspect_charts(file, edited):
    presentation = Presentation(file)
    slide, = presentation.slides
    records = []
    for shape in slide.shapes:
        if not shape.has_chart:
            continue
        chart = shape.chart
        categories = [category.label for category in chart.plots[0].categories]
        values = [list(series.values) for series in chart.series]
        workbook = load_workbook(BytesIO(chart.part.chart_workbook.xlsx_part.blob), data_only=True)
        try:
            assert [workbook.worksheets[0].cell(row, 1).value for row in range(2, len(categories)+2)] == categories
            for column, series in enumerate(values, 2):
                assert [workbook.worksheets[0].cell(row, column).value for row in range(2, len(categories)+2)] == series
            provenance = list(workbook["PDF Recovery"].values)[1:]
            estimated = sum(str(row[4]).startswith("Estimated") for row in provenance)
        finally:
            workbook.close()
        axes = chart._chartSpace.xpath(".//c:valAx")
        records.append({
            "categories": categories, "series": values, "estimated": estimated, "axes": len(axes),
            "primary_range": [chart.value_axis.minimum_scale, chart.value_axis.maximum_scale],
            "markers": [symbol.get("val") for symbol in chart._chartSpace.xpath(".//c:ser/c:marker/c:symbol")
                        if symbol.get("val") != "none"],
        })
        # Exercise editing the series and the embedded workbook together.
        replacement = CategoryChartData()
        replacement.categories = categories
        for index, (series, points) in enumerate(zip(chart.series, values, strict=True)):
            replacement.add_series(series.name, [value + index + 1 for value in points])
        chart.replace_data(replacement)
    presentation.save(edited)
    reopened = Presentation(edited)
    for shape, prior in zip((s for s in reopened.slides[0].shapes if s.has_chart), records, strict=True):
        assert [list(series.values) for series in shape.chart.series] == [
            [value+index+1 for value in series] for index, series in enumerate(prior["series"])]
        workbook = load_workbook(BytesIO(shape.chart.part.chart_workbook.xlsx_part.blob), data_only=True)
        try:
            assert workbook.worksheets[0]["B2"].value == prior["series"][0][0]+1
        finally:
            workbook.close()
    return {"chart_count":len(records),"non_chart_objects":len(slide.shapes)-len(records),"charts":records}


def paint_sources(folder):
    yield from (path for path in pdf_fixtures(folder) if path.stem in ("clipping", "soft-mask", "pattern"))
    for name in ("rectangle-clip", "normal-alpha", "multiply", "multiply-with-caption"):
        path = folder / f"{name}.pdf"
        with fitz.open() as pdf:
            page = pdf.new_page(width=240, height=180)
            if name == "rectangle-clip":
                add_stream(pdf, page, b"q 60 30 80 50 re W n 0.2 0.7 0.5 rg 20 15 100 50 re f Q")
            else:
                blend = "/Multiply" if name.startswith("multiply") else "/Normal"
                pdf.xref_set_key(page.xref, "Resources", f"<< /ExtGState << /A << /ca 0.5 /BM {blend} >> >> >>")
                add_stream(pdf, page, b"0 0.5 1 rg 25 40 105 90 re f q /A gs 1 0 0 rg 70 25 90 90 re f Q")
                if name == "multiply-with-caption":
                    page.insert_text((168, 28), "Live caption", fontsize=10)
                    page.draw_rect((190, 155, 225, 170), color=None, fill=(0, .8, .25))
            pdf.save(path)
        yield path


    for name in ("axial-mask", "layered-pattern"):
        path = folder / f"{name}.pdf"
        with fitz.open() as pdf:
            page = pdf.new_page(width=240, height=180)
            resource = pdf.get_new_xref()
            if name == "axial-mask":
                pdf.update_object(resource, "<< /Type /XObject /Subtype /Form /BBox [20 30 220 150] "
                                  "/Group << /S /Transparency /CS /DeviceGray >> /Resources << /Shading "
                                  "<< /G << /ShadingType 2 /ColorSpace /DeviceGray /Coords [20 30 220 30] "
                                  "/Extend [true true] /Function << /FunctionType 2 /Domain [0 1] "
                                  "/C0 [0.1] /C1 [0.9] /N 1 >> >> >> >> >>")
                pdf.update_stream(resource, b"/G sh")
                resources = f"<< /ExtGState << /M << /SMask << /S /Luminosity /G {resource} 0 R >> >> >> >>"
                content = b"q /M gs 1 0.15 0.05 rg 0 0 240 180 re f Q"
            else:
                pdf.update_object(resource, "<< /Type /Pattern /PatternType 1 /PaintType 1 /TilingType 1 "
                                  "/BBox [0 0 20 15] /Matrix [1 0 0 1 4 3] /XStep 20 /YStep 15 /Resources <<>> >>")
                pdf.update_stream(resource, b".92 .1 .2 rg 0 0 9 7 re 10 8 6 6 re f 0 .2 .9 rg 10 0 8 7 re f")
                resources = f"<< /Pattern << /P {resource} 0 R >> >>"
                content = b"q 18 24 184 111 re W n /Pattern cs /P scn 12 18 205 131 re f Q"
            pdf.xref_set_key(page.xref, "Resources", resources)
            add_stream(pdf, page, content)
            pdf.save(path)
        yield path


def inspect_paint(path):
    slide, = Presentation(path).slides
    objects = []
    for shape in slide.shapes:
        record = {
            "kind": shape.shape_type.name,
            "bounds_pt": [round(getattr(shape, dimension).pt, 2) for dimension in ("left", "top", "width", "height")],
            "opacity": [int(alpha.get("val")) for alpha in shape._element.xpath(".//a:alpha")],
        }
        if shape.has_text_frame and shape.text:
            record["text"] = shape.text
        if shape.shape_type == MSO_SHAPE_TYPE.PICTURE:
            with Image.open(BytesIO(shape.image.blob)) as image:
                channel = image.convert("RGBA").getchannel("A")
                record["alpha"] = channel.getextrema()
                record["image_size"] = image.size
        objects.append(record)
    return objects


def pdf_text(path):
    with fitz.open(path) as document:
        return " ".join(page.get_text().strip() for page in document)


def transformed_group(folder, *, forced_break=False, mixed=False):
    stem = "transformed-mixed" if mixed else "transformed-explicit" if forced_break else "transformed"
    seed, out = folder/f"{stem}-seed.docx", folder/f"{stem}.docx"
    grouped_word(seed, folder/"picture.png")
    with ZipFile(seed) as source, ZipFile(out,"w") as target:
        for entry in source.infolist():
            data = source.read(entry)
            if entry.filename == "word/document.xml":
                root = etree.fromstring(data)
                group, = root.xpath(".//*[local-name()='wgp']")
                rect = group.xpath("./*[local-name()='wsp']")[0]
                xfrm, = rect.xpath("./*[local-name()='spPr']/*[local-name()='xfrm']")
                xfrm.set("rot", "1800000")
                picture, = group.xpath("./*[local-name()='pic']")
                transform, = picture.xpath("./*[local-name()='spPr']/*[local-name()='xfrm']")
                transform.set("flipH", "1")
                props, = rect.xpath("./*[local-name()='bodyPr']")
                text = etree.fromstring(b'''
                    <wps:txbx xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape"
                        xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:txbxContent>
                        <w:p><w:pPr><w:jc w:val="center"/></w:pPr><w:r><w:rPr><w:b/><w:sz w:val="16"/></w:rPr>
                        <w:t>Editable</w:t></w:r></w:p></w:txbxContent></wps:txbx>''')
                rect.insert(rect.index(props),text)
                if forced_break:
                    run, = text.xpath(".//*[local-name()='r']")
                    line, = run.xpath("./*[local-name()='t']")
                    line.text = "Upper"
                    run.append(etree.Element(line.tag.replace("}t", "}br")))
                    etree.SubElement(run, line.tag).text = "Lower"
                if mixed:
                    run, = text.xpath(".//*[local-name()='r']")
                    other = deepcopy(run)
                    ns = "{" + run.nsmap["w"] + "}"
                    for index, (item, word, font, rgb) in enumerate((
                        (run, "RED", "Arial", "DE092A"),
                        (other, "BLUE", "Times New Roman", "0031D8"),
                    )):
                        for child in list(item)[1:]:
                            item.remove(child)
                        properties = item[0]
                        fonts = etree.SubElement(properties, ns+"rFonts")
                        for key in ("ascii", "hAnsi", "eastAsia"):
                            fonts.set(ns+key, font)
                        etree.SubElement(properties, ns+"color").set(ns+"val", rgb)
                        if index:
                            etree.SubElement(item, ns+"br")
                        etree.SubElement(item, ns+"t").text = word
                    run.getparent().append(other)
                props.set("anchor","ctr")
                for key in ("lIns","rIns","tIns","bIns"):
                    props.set(key,"12700")
                data = etree.tostring(root)
            target.writestr(entry,data)
    return out


def inspect_group(file):
    with ZipFile(file) as archive:
        root = etree.fromstring(archive.read("Contents/section0.xml"))
        find = lambda tag: root.xpath(f".//*[local-name()='{tag}']")
        groups = find("container")
        rotations = [int(node.get("angle")) for node in find("rotationInfo")]
        flips = [node.get("horizontal") for node in find("flip")]
        boxes = ["".join(node.itertext()) for node in find("drawText")]
        rotated_text = [
            {"text": art.get("text"), "angle": int(art.find("{*}rotationInfo").get("angle"))}
            for art in find("textart")
        ]
        return {"groups":len(groups),"rotation":rotations,"horizontal_flip":flips,"text_boxes":boxes,
                "rotated_text":rotated_text}


def main():
    parser = argparse.ArgumentParser()
    for name in ("baseline","worker","rhwp","output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--render", action="store_true")
    parser.add_argument("--render-word", action="store_true", help="Render the HWPX cases without using PowerPoint.")
    args = parser.parse_args()
    root = args.output.resolve()
    inputs = root/"inputs"
    inputs.mkdir(parents=True)
    picture = Image.new("RGB", (40, 30), "orange")
    ImageDraw.Draw(picture).rectangle((0, 0, 11, 29), fill="blue")
    picture.save(inputs / "picture.png")
    results = {"worker_sha256":digest(args.worker),"cases":{}}
    baseline = [sys.executable,"-B",args.baseline.resolve()/"run_magic_converter.py"]
    commands = {"python":(baseline,None),"rust":([args.worker.resolve()],native_environment())}

    def convert(source, action):
        original = sha256(source.read_bytes()).hexdigest()
        row = results["cases"][source.stem] = {"source_sha256":original}
        outputs = {}
        for edition,(command,env) in commands.items():
            folder = root/source.stem/edition
            code, receipt = run(command,("convert",source,"--action",action,"--output",folder,
                "--data-dir",root/f"profile-{edition}"),environment=env)
            assert code == 0, receipt
            outputs[edition] = Path(receipt["jobs"][0]["outputs"][0])
            row[edition] = {"warnings":receipt["jobs"][0]["warnings"]}
        assert digest(source) == original
        return row, outputs

    def save():
        (root/"report.json").write_text(json.dumps(results,indent=2,ensure_ascii=False),encoding="utf-8")

    def render(output, label):
        report = output.with_name(f"{output.stem}-render.pdf")
        command = ([args.worker.resolve(),"office-worker","powerpoint",output,report] if label=="slides"
                   else [args.rhwp.resolve(),"export-pdf",output,"-o",report])
        for attempt in range(4):
            completed = subprocess.run(command, capture_output=True, timeout=120)
            if completed.returncode == 0:
                return report
            message = completed.stderr.decode("utf-8", errors="replace")
            if "기존 Office 서버(PowerPoint)" not in message or attempt == 3:
                raise RuntimeError(message or completed.stdout.decode("utf-8", errors="replace"))
            time.sleep(2)
        return report

    for source, count in chart_sources(inputs):
        row, outputs = convert(source,"pptx_editable")
        for edition,path in outputs.items():
            row[edition].update(inspect_charts(path,path.with_name("edited.pptx")))
        assert row["python"]["chart_count"] == count, row
        row["expected_chart_count"] = count
        row["same_chart_data"] = row["python"]["charts"] == row["rust"]["charts"]
        if source.stem == "combo":
            row["same_object_count"] = row["python"]["non_chart_objects"] == row["rust"]["non_chart_objects"]
        if source.stem == "shared-text":
            for path in outputs.values():
                slide, = Presentation(path).slides
                assert any(s.has_text_frame and "Outside caption" in s.text for s in slide.shapes)
        if args.render and source.stem in ("columns","combo","grouped-percent"):
            row["render"] = compare_pdf(*(render(path,"slides") for path in outputs.values()))
        if args.render and source.stem == "columns":
            edited_pdf = render(outputs["rust"].with_name("edited.pptx"), "slides")
            row["edited_render_text"] = pdf_text(edited_pdf)
        print(source.stem, row["rust"]["chart_count"], row["same_chart_data"],flush=True)
        save()
    for source in paint_sources(inputs):
        row, outputs = convert(source,"pptx_editable")
        for edition, path in outputs.items():
            row[edition]["objects"] = inspect_paint(path)
        if source.stem == "clipping":
            shape, = row["rust"]["objects"]
            assert shape["kind"] in ("FREEFORM", "AUTO_SHAPE"), row
        if source.stem == "rectangle-clip":
            shape, = row["rust"]["objects"]
            assert shape["kind"] == "AUTO_SHAPE" and shape["bounds_pt"] == [60.0, 115.0, 60.0, 35.0], row
        if source.stem == "normal-alpha":
            shapes = row["rust"]["objects"]
            assert len(shapes) == 2 and all(s["kind"] == "AUTO_SHAPE" for s in shapes), row
            assert len(shapes[1]["opacity"]) == 1 and 49_000 <= shapes[1]["opacity"][0] <= 51_000, row
        if source.stem == "pattern":
            shape, = row["rust"]["objects"]
            assert shape["kind"] == "FREEFORM", row
        if source.stem == "soft-mask":
            shape, = row["rust"]["objects"]
            assert shape["kind"] == "AUTO_SHAPE" and shape["bounds_pt"] == [20.0, 70.0, 100.0, 80.0], row
            assert len(shape["opacity"]) == 1 and 49_000 <= shape["opacity"][0] <= 51_000, row
        if source.stem in ("pattern", "soft-mask"):
            edited = outputs["rust"].with_name("edited.pptx")
            document = Presentation(outputs["rust"])
            shape, = document.slides[0].shapes
            shape.left += 91440
            shape.fill.fore_color.rgb = RGBColor(0, 64, 255)
            document.save(edited)
            changed, = Presentation(edited).slides[0].shapes
            assert changed.left == shape.left and changed.fill.fore_color.rgb == RGBColor(0, 64, 255)
            row["rust"]["edited"] = inspect_paint(edited)
        if source.stem == "axial-mask":
            shape, = row["rust"]["objects"]
            assert shape["kind"] == "AUTO_SHAPE" and shape["opacity"] == [10000, 90000], row
        if source.stem == "layered-pattern":
            assert [shape["kind"] for shape in row["rust"]["objects"]] == ["FREEFORM", "FREEFORM"], row
        if source.stem == "multiply":
            shape, = row["rust"]["objects"]
            assert shape["kind"] == "PICTURE" and shape["alpha"] == (0, 255), row
            assert row["python"]["objects"] == row["rust"]["objects"], row
        if source.stem == "multiply-with-caption":
            raster, caption, vector = row["rust"]["objects"]
            assert raster["kind"] == "PICTURE" and caption["text"] == "Live caption", row
            assert vector["kind"] == "AUTO_SHAPE", row
        if args.render:
            renders = {edition:render(path,"slides") for edition,path in outputs.items()}
            row["render"] = compare_pdf(*renders.values())
            row["source_to_render"] = {edition:compare_pdf(source,path) for edition,path in renders.items()}
        save()
    for forced in (False, True):
        source = transformed_group(inputs, forced_break=forced)
        row, outputs = convert(source,"document:hwpx")
        for edition,path in outputs.items():
            row[edition].update(inspect_group(path))
        assert row["rust"]["groups"] == 2 and "1" in row["rust"]["horizontal_flip"], row
        art, = row["rust"]["rotated_text"]
        expected = art["text"]
        assert art["angle"] == 330 and len(expected.splitlines()) == 2, row
        assert expected == "Upper\nLower" if forced else expected.replace("\n", "") == "Editable", row
        if args.render or args.render_word:
            renders = {}
            for edition, path in outputs.items():
                command, env = commands[edition]
                code, receipt = run(command, ("convert",path,"--action","document:pdf","--output",path.parent/"pdf",
                                               "--data-dir",root/f"profile-{edition}"), environment=env)
                assert code == 0, receipt
                renders[edition] = Path(receipt["jobs"][0]["outputs"][0])
            row["render"] = compare_pdf(*renders.values())
            row["render_text"] = {edition:pdf_text(path) for edition,path in renders.items()}
            for edition, path in renders.items():
                with fitz.open(path) as pdf:
                    page = pdf[0]
                    row[edition]["text_matches"] = {
                        word: len(page.search_for(word, quads=True)) for word in expected.splitlines()
                    }
                    row[edition]["directions"] = [
                        list(line["dir"]) for block in page.get_text("dict")["blocks"]
                        for line in block.get("lines", [])
                        if "".join(span["text"] for span in line["spans"]) in expected.splitlines()
                    ]
                    rgb = page.get_pixmap()
                    row[edition]["picture_stripes"] = [
                        [list(rgb.pixel(round(box[0] + (box[2]-box[0])*fraction), round((box[1]+box[3])/2)))
                         for fraction in (.12, .87)]
                        for info in page.get_image_info()
                        if (box := info["bbox"]) and info["width"] == 40 and info["height"] == 30
                    ]
            assert all(row["rust"]["text_matches"].values()), row
            assert len(row["rust"]["directions"]) == 2, row
            assert all(abs(direction[1] - 0.5) < .03 for direction in row["rust"]["directions"]), row
            assert row["rust"]["picture_stripes"] == [[[255,165,0],[0,0,255]]], row
            if forced:
                edited = outputs["rust"].with_name("edited-text.hwpx")
                with ZipFile(outputs["rust"]) as original, ZipFile(edited, "w") as target:
                    for member in original.infolist():
                        data = original.read(member)
                        if member.filename == "Contents/section0.xml":
                            doc = etree.fromstring(data)
                            textart, = doc.xpath(".//*[local-name()='textart']")
                            textart.set("text", "테스트\n수정")
                            data = etree.tostring(doc, encoding="UTF-8")
                        target.writestr(copy(member), data)
                command, env = commands["rust"]
                code, receipt = run(command, ("convert", edited, "--action", "document:pdf",
                                               "--output", edited.parent/"pdf", "--data-dir", root/"profile-rust"),
                                    environment=env)
                assert code == 0, receipt
                with fitz.open(receipt["jobs"][0]["outputs"][0]) as document:
                    page = document[0]
                    assert page.search_for("테스트") and page.search_for("수정"), page.get_text()
                    row["rust"]["edited_pdf_text"] = page.get_text()
                code, receipt = run(command, ("convert", source, "--action", "document:hwp",
                                               "--output", edited.parent/"legacy", "--data-dir", root/"profile-rust"),
                                    environment=env)
                assert code == 0, receipt
                legacy = Path(receipt["jobs"][0]["outputs"][0])
                code, receipt = run(command, ("convert", legacy, "--action", "document:pdf",
                                               "--output", legacy.parent, "--data-dir", root/"profile-rust"),
                                    environment=env)
                assert code == 0, receipt
                with fitz.open(receipt["jobs"][0]["outputs"][0]) as document:
                    images = document[0].get_image_info()
                    assert all(document[0].search_for(word, quads=True) for word in ("Upper", "Lower")), document[0].get_text()
                    row["rust"]["legacy_hwp_image_count"] = len(images)
        command, env = commands["rust"]
        output = outputs["rust"].parent
        code, receipt = run(command, ("convert", outputs["rust"], "--action", "document:txt",
                                      "--output", output/"txt", "--data-dir", root/"profile-rust"),
                            environment=env)
        assert code == 0, receipt
        recovered = Path(receipt["jobs"][0]["outputs"][0]).read_text(encoding="utf-8-sig")
        row["recovered_text"] = recovered
        assert expected in recovered, row
        save()
    source = transformed_group(inputs, mixed=True)
    row, outputs = convert(source, "document:hwpx")
    hwpx = outputs["rust"]
    with ZipFile(hwpx) as archive:
        section = etree.fromstring(archive.read("Contents/section0.xml"))
        arts = section.xpath(".//*[local-name()='textart']")
        styles = [(art.get("text"), art.find("{*}textartPr").get("fontName"),
                   art.find(".//{*}winBrush").get("faceColor").lower()) for art in arts]
        assert styles == [("RED", "Arial", "#de092a"), ("BLUE", "Times New Roman", "#0031d8")], styles
        row["rust"]["styles"] = styles
    if args.render or args.render_word:
        command, env = commands["rust"]
        def convert_rust(path, action, folder):
            code, receipt = run(command, ("convert", path, "--action", action, "--output", folder,
                                          "--data-dir", root_dir/"profile-rust"), environment=env)
            assert code == 0, receipt
            return Path(receipt["jobs"][0]["outputs"][0])
        root_dir = args.output.resolve()
        hwp = convert_rust(source, "document:hwp", hwpx.parent/"legacy")
        for file in (hwpx, hwp):
            pdf = convert_rust(file, "document:pdf", file.parent/"pdf")
            with fitz.open(pdf) as document:
                page = document[0]
                matches = [page.search_for(word, quads=True) for word in ("RED", "BLUE")]
                assert all(len(match) == 1 and match[0].rect.get_area() > 1 for match in matches), matches
                spans = [span for block in page.get_text("dict")["blocks"] for line in block.get("lines", [])
                         for span in line["spans"] if span["text"] in ("RED", "BLUE")]
                assert [(span["text"], span["color"]) for span in spans] == [("RED", 0xde092a), ("BLUE", 0x0031d8)], spans
                # Project the origins onto the axis perpendicular to the baseline.
                # An earlier build placed both runs at the origin of their group.
                dx, dy = (b-a for a,b in zip(spans[0]["origin"],spans[1]["origin"]))
                assert abs(-0.5 * dx + 0.8660254 * dy) > 4, spans
                row["rust"][file.suffix] = [span["origin"] for span in spans]
    results["status"]="complete"
    save()


if __name__ == "__main__":
    main()
