
from __future__ import annotations

import copy
import html
import io
import hashlib
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET
from PIL import Image

from docx import Document
from docx.shared import Pt, RGBColor
from docx.table import Table as WordTable
from docx.text.paragraph import Paragraph as WordParagraph

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs.raw_api import bounded_raw

NS = {key: f"http://www.hancom.co.kr/hwpml/2011/{name}" for key, name in
      (("hp", "paragraph"), ("hs", "section"), ("hh", "head"), ("hc", "core"))}
NS["opf"] = "http://www.idpf.org/2007/opf/"
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)
MAX_XML = 64 * 1024 * 1024


def q(prefix: str, name: str) -> str:
    return f"{{{NS[prefix]}}}{name}"


def local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


@dataclass(frozen=True)
class TextRun:
    text: str
    size: float = 10
    bold: bool = False
    italic: bool = False
    color: str = "000000"
    font: str = "맑은 고딕"
    spacing_percent: float = 0
    width_percent: float = 100
    spacing_pt: float = 0
    proportional_tracking_allowed: bool = True


@dataclass
class Picture:
    data: bytes
    width: float
    height: float
    name: str = "image.png"
    crop: tuple[float, float, float, float] = (0, 0, 0, 0)

    @property
    def text(self) -> str:
        return ""


@dataclass
class Equation:
    script: str
    omml: bytes | None = None
    size: float = 10
    supported: bool = True
    box: tuple[float, float, float] | None = None  # width/height in pt, baseline %

    @property
    def text(self) -> str:
        return self.script


@dataclass
class Paragraph:
    runs: list[TextRun | Picture | Equation] = field(default_factory=list)
    alignment: str = "left"
    page_break_before: bool = False
    left_indent: float = 0
    right_indent: float = 0
    first_line_indent: float = 0
    space_before: float = 0
    space_after: float = 0

    @property
    def text(self) -> str:
        return "".join(run.text for run in self.runs)


@dataclass
class Cell:
    row: int
    col: int
    paragraphs: list[Paragraph | Table]
    rowspan: int = 1
    colspan: int = 1
    fill: str | None = None


@dataclass
class Table:
    rows: int
    cols: int
    cells: list[Cell]
    col_widths: list[float] | None = None
    row_heights: list[float] | None = None

    @property
    def text(self) -> str:
        return "\n".join("\t".join("\n".join(p.text for p in c.paragraphs)
                                   for c in sorted(self.cells, key=lambda c: c.col) if c.row == row)
                         for row in range(self.rows))


@dataclass
class DocumentData:
    blocks: list[Paragraph | Table] = field(default_factory=list)
    width: float = 595.28
    height: float = 841.86
    margins: tuple[float, float, float, float] = (72, 72, 72, 72)
    warnings: list[str] = field(default_factory=list)
    headers: list[Paragraph | Table] = field(default_factory=list)
    footers: list[Paragraph | Table] = field(default_factory=list)

    def text(self) -> str:
        pieces = []
        for block in [*self.headers, *self.blocks, *self.footers]:
            if isinstance(block, Paragraph):
                pieces.append(block.text)
            else:
                for row in range(block.rows):
                    pieces.append("\t".join("\n".join(p.text for p in cell.paragraphs)
                                            for cell in sorted(block.cells, key=lambda c: c.col) if cell.row == row))
        return "\n".join(pieces)


def _xml(archive: zipfile.ZipFile, name: str) -> ET.Element:
    info = archive.getinfo(name)
    if info.file_size > MAX_XML:
        raise ConversionError(f"HWPX XML이 너무 큽니다: {name}")
    data = archive.read(info)
    if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
        raise ConversionError("외부 엔터티를 포함한 HWPX는 읽지 않습니다.")
    return ET.fromstring(data)


def _section_names(archive: zipfile.ZipFile) -> list[str]:
    names = {n for n in archive.namelist() if re.fullmatch(r"Contents/section\d+\.xml", n)}
    ordered: list[str] = []
    if "Contents/content.hpf" in archive.namelist():
        package = _xml(archive, "Contents/content.hpf")
        items = {item.get("id"): item.get("href", "") for item in package.iter() if local(item) == "item"}
        for ref in package.iter():
            if local(ref) == "itemref":
                href = items.get(ref.get("idref"), "").lstrip("./")
                candidate = href if href.startswith("Contents/") else "Contents/" + href
                if candidate in names and candidate not in ordered:
                    ordered.append(candidate)
    return ordered + sorted(names - set(ordered), key=lambda n: int(re.search(r"section(\d+)", n)[1]))


@bounded_raw
def read_hwpx(source: Path) -> DocumentData:
    from magic_converter.core.documents.rich_hwp import read_hwpx_rich
    return read_hwpx_rich(source)


def decode_hwp_text(payload: bytes) -> str:
    """Skip HWP eight-WCHAR inline/extended controls, retaining tabs/newlines."""
    output: list[bytes] = []
    pos = 0
    wide_controls = {1, 2, 3, 4, 5, 6, 7, 8, 9, 11, 12, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23}
    while pos + 2 <= len(payload):
        value = struct.unpack_from("<H", payload, pos)[0]
        if value in wide_controls:
            if value == 9:
                output.append(b"\x09\x00")
            pos += 16
        else:
            if value in {10, 13}:
                output.append(b"\x0a\x00")
            elif value >= 32:
                output.append(payload[pos:pos + 2])
            elif value == 24:
                output.append(b"-\x00")
            elif value in {30, 31}:
                output.append(b" \x00")
            pos += 2
    return b"".join(output).decode("utf-16le", errors="replace").rstrip("\n")


def hwp_records(data: bytes):
    pos = 0
    while pos < len(data):
        if len(data) - pos < 4:
            raise ConversionError("HWP 레코드 헤더가 잘렸습니다.")
        value = struct.unpack_from("<I", data, pos)[0]
        tag, size = value & 0x3FF, value >> 20
        pos += 4
        if size == 0xFFF:
            if len(data) - pos < 4:
                raise ConversionError("HWP 확장 레코드 헤더가 잘렸습니다.")
            size = struct.unpack_from("<I", data, pos)[0]
            pos += 4
        if size > len(data) - pos:
            raise ConversionError("HWP 레코드 본문이 잘렸습니다.")
        yield tag, data[pos:pos + size]
        pos += size


@bounded_raw
def read_hwp(source: Path) -> DocumentData:
    from magic_converter.core.documents.rich_hwp import read_hwp_rich
    return read_hwp_rich(source)


@bounded_raw
def read_docx(source: Path) -> DocumentData:
    from magic_converter.core.documents.rich_hwp import read_docx_rich
    return read_docx_rich(source)


@bounded_raw
def write_docx(data: DocumentData, destination: Path) -> None:
    from magic_converter.core.documents.rich_hwp import write_docx_rich
    return write_docx_rich(data, destination)


@bounded_raw
def write_hwpx(data: DocumentData, destination: Path) -> None:
    pass
    template = Path(__file__).resolve().parents[2] / "data" / "hwpx" / "Skeleton.hwpx"
    with zipfile.ZipFile(template) as archive:
        files = {name: archive.read(name) for name in archive.namelist()}
    def serialize(root: ET.Element, seed: bytes) -> bytes:
        # even for namespaces not currently used by an element. ElementTree
        # would otherwise drop the unused declarations during serialization.
        namespaces = dict(value for _, value in ET.iterparse(io.BytesIO(seed), events=("start-ns",)))
        for prefix, uri in namespaces.items():
            ET.register_namespace(prefix, uri)
        text = ET.tostring(root, encoding="unicode")
        end = text.index(">")
        opening = text[:end]
        missing = "".join(f' xmlns:{prefix}="{html.escape(uri, quote=True)}"' for prefix, uri in namespaces.items()
                          if prefix and f"xmlns:{prefix}=" not in opening)
        return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
                + opening + missing + text[end:]).encode("utf-8")
    header, section = ET.fromstring(files["Contents/header.xml"]), ET.fromstring(files["Contents/section0.xml"])
    first_run = copy.deepcopy(section.find(q("hp", "p")).find(q("hp", "run")))
    section.clear()
    page = first_run.find(".//" + q("hp", "pagePr"))
    page.set("width", str(round(data.width * 100)))
    page.set("height", str(round(data.height * 100)))
    page_margin = page.find(q("hp", "margin"))
    for key, value in zip(("left", "right", "top", "bottom"), data.margins):
        page_margin.set(key, str(round(value * 100)))
    chars = header.find(".//" + q("hh", "charProperties"))
    base_char = copy.deepcopy(chars[0])
    style_ids: dict[tuple, str] = {}
    fontfaces = header.find(".//" + q("hh", "fontfaces"))
    fonts: dict[str, str] = {}
    # Existing style references stay valid; new run styles are appended.
    counter = 0
    image_assets = {}
    para_properties = header.find(".//" + q("hh", "paraProperties"))
    paragraph_styles = {("left", 0, 0, 0, 0, 0): "0"}

    def child(parent, tag: str, **attrs):
        return ET.SubElement(parent, q("hp", tag), {k: str(v) for k, v in attrs.items()})

    def style(run: TextRun) -> str:
        from magic_converter.core.documents.character_spacing import hwpx_spacing
        tracking = hwpx_spacing(run, data.warnings)
        key = (run.size, run.bold, run.italic, run.color, run.font, tracking, run.width_percent)
        if key in style_ids:
            return style_ids[key]
        if run.font not in fonts:
            identifier = str(max(int(f.get("fontCnt", "0")) for f in fontfaces))
            for face in fontfaces:
                node = copy.deepcopy(face[0])
                node.set("id", identifier)
                node.set("face", run.font)
                face.append(node)
                face.set("fontCnt", str(len(face)))
            fonts[run.font] = identifier
        node = copy.deepcopy(base_char)
        identifier = str(max(int(c.get("id", "0")) for c in chars) + 1)
        node.set("id", identifier)
        node.set("height", str(round(max(1, run.size) * 100)))
        node.set("textColor", "#" + (run.color if re.fullmatch(r"[0-9A-Fa-f]{6}", run.color) else "000000"))
        for tag in ("bold", "italic"):
            for old in node.findall(q("hh", tag)):
                node.remove(old)
            if getattr(run, tag):
                ET.SubElement(node, q("hh", tag))
        fontref = node.find(q("hh", "fontRef"))
        for name in fontref.attrib:
            fontref.set(name, fonts[run.font])
        for tag, value in (("spacing", tracking), ("ratio", run.width_percent)):
            metric = node.find(q("hh", tag))
            if metric is not None:
                for name in metric.attrib:
                    metric.set(name, str(round(value)))
        chars.append(node)
        chars.set("itemCnt", str(len(chars)))
        style_ids[key] = identifier
        return identifier

    def para_style(source):
        from magic_converter.core.documents.paragraph_layout import FIELDS, HWPX_FIELDS
        key = (source.alignment, *(getattr(source, name) for name in FIELDS))
        if key not in paragraph_styles:
            node = copy.deepcopy(para_properties[0])
            identifier = str(max(int(p.get("id", "0")) for p in para_properties) + 1)
            node.set("id", identifier)
            align = node.find(q("hh", "align"))
            if align is not None:
                align.set("horizontal", source.alignment.upper())
            for margin in node.iter(q('hh', 'margin')):
                for name, tag in HWPX_FIELDS.items():
                    metric = margin.find(q('hc', tag))
                    if metric is not None:
                        metric.set('value', str(round(getattr(source, name) * 100)))
                        metric.set('unit', 'HWPUNIT')
            para_properties.append(node)
            para_properties.set("itemCnt", str(len(para_properties)))
            paragraph_styles[key] = identifier
        return paragraph_styles[key]

    def picture(parent, source: Picture, available):
        nonlocal counter
        from magic_converter.core.documents.rich_hwp import _picture_bytes
        raw = _picture_bytes(source)
        with Image.open(io.BytesIO(raw)) as decoded:
            pixel_width, pixel_height = decoded.size
            extension = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "BMP": "bmp", "TIFF": "tiff"}.get(decoded.format, "png")
        identity = hashlib.sha256(raw).hexdigest()
        if identity not in image_assets:
            identifier = f"image{len(image_assets) + 1}"
            name = f"BinData/{identifier}.{extension}"
            files[name] = raw
            image_assets[identity] = (identifier, name, "image/" + ("jpeg" if extension == "jpg" else extension))
        identifier = image_assets[identity][0]
        scale = min(1, available / max(1, source.width * 100))
        width, height = max(1, round(source.width * 100 * scale)), max(1, round(source.height * 100 * scale))
        ow, oh = pixel_width * 75, pixel_height * 75
        counter += 1
        pic = child(parent, "pic", id=counter + 200000, zOrder=0, numberingType="PICTURE", textWrap="TOP_AND_BOTTOM",
                    textFlow="BOTH_SIDES", lock=0, dropcapstyle="None", reverse=0)
        child(pic, "offset", x=0, y=0)
        child(pic, "orgSz", width=ow, height=oh)
        child(pic, "curSz", width=width, height=height)
        child(pic, "flip", horizontal=0, vertical=0)
        child(pic, "rotationInfo", angle=0, centerX=width // 2, centerY=height // 2, rotateimage=1)
        rendering = child(pic, "renderingInfo")
        for tag in ("transMatrix", "scaMatrix", "rotMatrix"):
            ET.SubElement(rendering, q("hc", tag), e1="1", e2="0", e3="0", e4="0", e5="1", e6="0")
        rect = child(pic, "imgRect")
        for i, (x, y) in enumerate(((0, 0), (ow, 0), (ow, oh), (0, oh))):
            ET.SubElement(rect, q("hc", f"pt{i}"), x=str(x), y=str(y))
        left, top, right, bottom = source.crop
        child(pic, "imgClip", left=round(ow * left), top=round(oh * top), right=round(ow * (1 - right)), bottom=round(oh * (1 - bottom)))
        child(pic, "inMargin", left=0, right=0, top=0, bottom=0)
        child(pic, "imgDim", dimwidth=ow, dimheight=oh)
        ET.SubElement(pic, q("hc", "img"), binaryItemIDRef=identifier, bright="0", contrast="0", effect="REAL_PIC", alpha="0")
        child(pic, "effects")
        child(pic, "sz", width=width, widthRelTo="ABSOLUTE", height=height, heightRelTo="ABSOLUTE", protect=0)
        child(pic, "pos", treatAsChar=1, affectLSpacing=0, flowWithText=1, allowOverlap=0, holdAnchorAndSO=0,
              vertRelTo="PARA", horzRelTo="COLUMN", vertAlign="TOP", horzAlign="LEFT", vertOffset=0, horzOffset=0)
        child(pic, "outMargin", left=0, right=0, top=0, bottom=0)
        child(pic, "shapeComment").text = source.name

    def paragraph(parent, source: Paragraph, available):
        nonlocal counter
        counter += 1
        p = child(parent, "p", id=counter, paraPrIDRef=para_style(source), styleIDRef=0, pageBreak=int(source.page_break_before), columnBreak=0, merged=0)
        if parent is section and len(section) == 1:
            p.append(first_run)
        from magic_converter.core.documents.character_spacing import metric_segments
        for item in metric_segments(source.runs or [TextRun("")],'hwpx'):
            if isinstance(item, Equation):
                from magic_converter.core.documents.document_math import measure
                width, height, baseline = item.box or measure(item)
                counter += 1
                eq = child(child(p, "run", charPrIDRef=0), "equation", id=counter + 400000,
                           zOrder=0, numberingType="EQUATION", textWrap="TOP_AND_BOTTOM", textFlow="BOTH_SIDES",
                           lock=0, dropcapstyle="None", version="Equation Version 60", baseLine=round(baseline),
                           textColor="#000000", baseUnit=round(item.size * 100), lineMode="CHAR", font="HYhwpEQ")
                child(eq, "sz", width=max(1, round(width * 100)), widthRelTo="ABSOLUTE",
                      height=max(1, round(height * 100)), heightRelTo="ABSOLUTE", protect=0)
                child(eq, "pos", treatAsChar=1, affectLSpacing=1, flowWithText=1, allowOverlap=0,
                      holdAnchorAndSO=0, vertRelTo="PARA", horzRelTo="COLUMN", vertAlign="TOP", horzAlign="LEFT", vertOffset=0, horzOffset=0)
                child(eq, "outMargin", left=0, right=0, top=0, bottom=0)
                # rhwp 0.8.7 requires a nonempty shapeComment.
                child(eq, "shapeComment").text = item.script or "수식"
                child(eq, "script").text = item.script
                continue
            if isinstance(item, Picture):
                picture(child(p, "run", charPrIDRef=0), item, available)
                continue
            run = child(p, "run", charPrIDRef=style(item))
            text = child(run, "t")
            parts = re.split(r"(\n|\t)", item.text.replace("\r\n", "\n"))
            text.text = parts[0]
            for i in range(1, len(parts), 2):
                control = child(text, "lineBreak" if parts[i] == "\n" else "tab")
                control.tail = parts[i + 1]
        return p

    borderfills = header.find(".//" + q("hh", "borderFills"))
    border_id = str(max(int(b.get("id", "0")) for b in borderfills) + 1)
    border = copy.deepcopy(borderfills[0])
    border.set("id", border_id)
    for name in ("leftBorder", "rightBorder", "topBorder", "bottomBorder"):
        edge = border.find(q("hh", name))
        if edge is not None:
            edge.attrib.update(type="SOLID", width="0.12 mm", color="#000000")
    borderfills.append(border)
    borderfills.set("itemCnt", str(len(borderfills)))
    fill_ids = {}
    def cell_border(fill):
        if not fill:
            return border_id
        if fill not in fill_ids:
            node = copy.deepcopy(border)
            identifier = str(max(int(b.get("id", "0")) for b in borderfills) + 1)
            node.set("id", identifier)
            brush = ET.SubElement(node, q("hc", "fillBrush"))
            ET.SubElement(brush, q("hc", "winBrush"), faceColor="#" + fill, hatchColor="#000000", alpha="0")
            borderfills.append(node)
            borderfills.set("itemCnt", str(len(borderfills)))
            fill_ids[fill] = identifier
        return fill_ids[fill]

    available_width = max(1000, round((data.width - data.margins[0] - data.margins[1]) * 100))
    def write_blocks(parent, content, available):
      for block in content or [Paragraph()]:
        if isinstance(block, Paragraph):
            paragraph(parent, block, available)
            continue
        anchor = paragraph(parent, Paragraph(), available)
        run = child(anchor, "run", charPrIDRef=0)
        widths = [w * 100 for w in block.col_widths] if block.col_widths else [available / block.cols] * block.cols
        scale = min(1, available / max(1, sum(widths)))
        widths = [w * scale for w in widths]
        heights = [max(1, h * 100) for h in block.row_heights] if block.row_heights else [1800] * block.rows
        table = child(run, "tbl", id=counter + 100000, zOrder=0, numberingType="TABLE", textWrap="TOP_AND_BOTTOM",
                      textFlow="BOTH_SIDES", lock=0, dropcapstyle="None", pageBreak="CELL", repeatHeader=1,
                      rowCnt=block.rows, colCnt=block.cols, cellSpacing=0, borderFillIDRef=border_id, noAdjust=0)
        child(table, "sz", width=round(sum(widths)), widthRelTo="ABSOLUTE", height=round(sum(heights)), heightRelTo="ABSOLUTE", protect=0)
        child(table, "pos", treatAsChar=1, affectLSpacing=0, flowWithText=1, allowOverlap=0, holdAnchorAndSO=0,
              vertRelTo="PARA", horzRelTo="COLUMN", vertAlign="TOP", horzAlign="LEFT", vertOffset=0, horzOffset=0)
        child(table, "outMargin", left=0, right=0, top=283, bottom=283)
        child(table, "inMargin", left=283, right=283, top=141, bottom=141)
        for r in range(block.rows):
            row = child(table, "tr")
            for cell in sorted((c for c in block.cells if c.row == r), key=lambda c: c.col):
                tc = child(row, "tc", name="", header=0, hasMargin=0, protect=0, editable=0, dirty=0, borderFillIDRef=cell_border(cell.fill))
                body = child(tc, "subList", id="", textDirection="HORIZONTAL", lineWrap="BREAK", vertAlign="CENTER",
                             linkListIDRef=0, linkListNextIDRef=0, textWidth=0, textHeight=0, hasTextRef=0, hasNumRef=0)
                width, height = sum(widths[cell.col:cell.col + cell.colspan]), sum(heights[cell.row:cell.row + cell.rowspan])
                write_blocks(body, cell.paragraphs, max(100, width - 566))
                child(tc, "cellAddr", colAddr=cell.col, rowAddr=cell.row)
                child(tc, "cellSpan", colSpan=cell.colspan, rowSpan=cell.rowspan)
                child(tc, "cellSz", width=round(width), height=round(height))
                child(tc, "cellMargin", left=283, right=283, top=141, bottom=141)
    write_blocks(section, data.blocks, available_width)
    first = section.find(q("hp", "p"))
    for name, content in (("header", data.headers), ("footer", data.footers)):
        if content:
            ctrl = child(child(first, "run", charPrIDRef=0), "ctrl")
            target = child(ctrl, name, id=counter + 300000, applyPageType="BOTH")
            body = child(target, "subList", id="", textDirection="HORIZONTAL", lineWrap="BREAK", vertAlign="TOP",
                         linkListIDRef=0, linkListNextIDRef=0, textWidth=0, textHeight=0, hasTextRef=0, hasNumRef=0)
            write_blocks(body, content, available_width)
    files["Contents/header.xml"] = serialize(header, files["Contents/header.xml"])
    files["Contents/section0.xml"] = serialize(section, files["Contents/section0.xml"])
    package = ET.fromstring(files["Contents/content.hpf"])
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    for meta in package.findall(".//" + q("opf", "meta")):
        meta.text = now if meta.get("name") in {"CreatedDate", "ModifiedDate", "date"} else ""
    manifest = package.find(q("opf", "manifest"))
    for identifier, name, mime in image_assets.values():
        ET.SubElement(manifest, q("opf", "item"), id=identifier, href=name, **{"media-type": mime, "isEmbeded": "1"})
    ET.SubElement(manifest, q("opf", "item"), id="version", href="version.xml", **{"media-type": "application/xml"})
    files["Contents/content.hpf"] = serialize(package, files["Contents/content.hpf"])
    files["Preview/PrvText.txt"] = data.text().encode("utf-8")
    # The seed preview is blank; don't ship it as a misleading document preview.
    files.pop("Preview/PrvImage.png", None)
    with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("mimetype", b"application/hwp+zip", compress_type=zipfile.ZIP_STORED)
        for name, content in files.items():
            if name != "mimetype":
                output.writestr(name, content)


def document_html(data: DocumentData) -> str:
    from magic_converter.core.documents.rich_hwp import document_html_rich
    return document_html_rich(data)
