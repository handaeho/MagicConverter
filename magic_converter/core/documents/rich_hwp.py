
from __future__ import annotations

import base64
import bisect
import html
import io
import re
import struct
import zipfile
import zlib
from dataclasses import dataclass, field, replace
from pathlib import Path, PurePosixPath
from urllib.parse import unquote
from xml.etree import ElementTree as ET

from PIL import Image

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs.raw_api import bounded_raw
from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.documents.hwp_documents import (Cell, DocumentData, Paragraph, Picture, Equation, Table, TextRun,
                               MAX_XML, _xml, _section_names, local, q)


def walk_blocks(blocks):
    for block in blocks:
        yield block
        if isinstance(block, Table):
            for cell in block.cells:
                yield from walk_blocks(cell.paragraphs)
        elif isinstance(block, Paragraph):
            yield from (run for run in block.runs if isinstance(run, (Picture, Equation)))


def object_counts(data: DocumentData) -> dict[str, int]:
    counts = {"tables": 0, "pictures": 0}
    for obj in walk_blocks([*data.headers, *data.blocks, *data.footers]):
        if isinstance(obj, Table):
            counts["tables"] += 1
        elif isinstance(obj, Picture):
            counts["pictures"] += 1
    return counts


def _dimensions(cells, rows, cols, sizes):
    """Recover grid dimensions from single and merged cell constraints."""
    widths, heights = [0.0] * cols, [0.0] * rows
    for cell, (width, height) in zip(cells, sizes):
        if cell.colspan == 1:
            widths[cell.col] = max(widths[cell.col], width)
        if cell.rowspan == 1:
            heights[cell.row] = max(heights[cell.row], height)
    for cell, (width, height) in zip(cells, sizes):
        for grid, start, span, total in ((widths, cell.col, cell.colspan, width),
                                          (heights, cell.row, cell.rowspan, height)):
            missing = [i for i in range(start, start + span) if not grid[i]]
            if missing:
                share = max(1, (total - sum(grid[start:start + span])) / len(missing))
                for i in missing:
                    grid[i] = share
    return [w or 72 for w in widths], [h or 12 for h in heights]


def _validate_grid(rows, cols, cells):
    if not (0 < rows <= 10000 and 0 < cols <= 1000 and rows * cols <= 100000):
        raise ConversionError("한글 표 크기가 허용 범위를 벗어났습니다.")
    occupied = set()
    for cell in cells:
        if (min(cell.row, cell.col) < 0 or min(cell.rowspan, cell.colspan) < 1
                or cell.row + cell.rowspan > rows or cell.col + cell.colspan > cols):
            raise ConversionError("한글 표 셀 주소가 잘못되었습니다.")
        for row in range(cell.row, cell.row + cell.rowspan):
            for col in range(cell.col, cell.col + cell.colspan):
                if (row, col) in occupied:
                    raise ConversionError("한글 표의 병합 셀이 겹칩니다.")
                occupied.add((row, col))


def _inflate(raw: bytes) -> bytes:
    decoder = zlib.decompressobj(-15)
    value = decoder.decompress(raw, MAX_XML + 1)
    if len(value) > MAX_XML or decoder.unconsumed_tail or not decoder.eof:
        raise ConversionError("HWP 압축 데이터가 손상되었거나 너무 큽니다.")
    return value


@dataclass
class Record:
    tag: int
    level: int
    data: bytes
    children: list[Record] = field(default_factory=list)

    def descendants(self):
        for child in self.children:
            yield child
            yield from child.descendants()


def records(raw: bytes):
    pos = 0
    while pos < len(raw):
        if len(raw) - pos < 4:
            raise ConversionError("HWP 레코드 헤더가 잘렸습니다.")
        value = struct.unpack_from("<I", raw, pos)[0]
        tag, level, size = value & 1023, (value >> 10) & 1023, value >> 20
        pos += 4
        if size == 4095:
            if len(raw) - pos < 4:
                raise ConversionError("HWP 확장 레코드가 잘렸습니다.")
            size = struct.unpack_from("<I", raw, pos)[0]
            pos += 4
        if size > len(raw) - pos:
            raise ConversionError("HWP 레코드 본문이 잘렸습니다.")
        yield Record(tag, level, raw[pos:pos + size])
        pos += size


def record_tree(raw: bytes) -> list[Record]:
    roots, stack = [], []
    for node in records(raw):
        while stack and stack[-1].level >= node.level:
            stack.pop()
        (stack[-1].children if stack else roots).append(node)
        stack.append(node)
    return roots


@bounded_raw
def read_hwp_rich(source: Path) -> DocumentData:
    import olefile
    result = DocumentData()
    with olefile.OleFileIO(source) as archive:
        header = archive.openstream("FileHeader").read()
        if len(header) < 40 or not header.startswith(b"HWP Document File"):
            raise ConversionError("HWP 5.x 문서가 아닙니다.")
        flags = struct.unpack_from("<I", header, 36)[0]
        if flags & 6:
            pass
        compressed = bool(flags & 1)

        def stream(path, inflate=True):
            if archive.get_size(path) > MAX_XML:
                raise ConversionError("HWP 스트림이 너무 큽니다.")
            raw = archive.openstream(path).read()
            return _inflate(raw) if compressed and inflate else raw

        docinfo = list(records(stream("DocInfo"))) if archive.exists("DocInfo") else []
        fonts = []
        styles = {}
        alignments = {}
        for node in docinfo:
            if node.tag == 19 and len(node.data) >= 3:
                size = struct.unpack_from("<H", node.data, 1)[0]
                fonts.append(node.data[3:3 + size * 2].decode("utf-16le", errors="replace"))
            elif node.tag == 21 and len(node.data) >= 56:
                font_id = struct.unpack_from("<H", node.data)[0]
                height, bits = struct.unpack_from("<II", node.data, 42)
                red, green, blue = node.data[52:55]
                styles[len(styles)] = dict(size=max(1, height / 100), bold=bool(bits & 2), italic=bool(bits & 1),
                                           color=f"{red:02X}{green:02X}{blue:02X}",
                                           font=fonts[font_id] if font_id < len(fonts) else "맑은 고딕")
            elif node.tag == 25 and len(node.data) >= 4:
                alignments[len(alignments)] = {0: "justify", 1: "left", 2: "right", 3: "center"}.get(
                    (struct.unpack_from("<I", node.data)[0] >> 2) & 7, "left")
        assets = {}
        for path in archive.listdir():
            if len(path) == 2 and path[0].casefold() == "bindata":
                match = re.match(r"BIN([0-9A-F]+)\.", path[1], re.I)
                if match:
                    assets[int(match[1], 16)] = path

        def picture(control: Record, node: Record) -> Picture:
            if len(node.data) < 73:
                raise ConversionError("HWP 그림 레코드가 잘렸습니다.")
            identifier = struct.unpack_from("<H", node.data, 71)[0]
            if identifier not in assets:
                raise ConversionError(f"HWP 그림 데이터를 찾지 못했습니다: {identifier}")
            path = assets[identifier]
            raw = stream(path, inflate=False)
            try:
                with Image.open(io.BytesIO(raw)) as image:
                    pixel_width, pixel_height = image.size
            except Exception:
                raw = _inflate(raw)
                try:
                    with Image.open(io.BytesIO(raw)) as image:
                        pixel_width, pixel_height = image.size
                except Exception as exc:
                    pass
            component = next((n for n in control.descendants() if n.tag == 76 and len(n.data) >= 32), None)
            offset = 28 if component is not None and component.data[:4] == component.data[4:8] else 24
            width, height = struct.unpack_from("<II", component.data, offset) if component and len(component.data) >= offset + 8 else (0, 0)
            width, height = width / 100 or pixel_width * 0.75, height / 100 or pixel_height * 0.75
            left, top, right, bottom = struct.unpack_from("<4i", node.data, 44)
            # Crop coordinates in HWP use the original image coordinate system.
            # Keep the image bytes and apply the visible crop at the output.
            crop = (max(0, left / right) if right else 0, max(0, top / bottom) if bottom else 0, 0, 0)
            return Picture(raw, width, height, path[1], crop)

        def control_blocks(control: Record, depth: int):
            if depth > 64:
                raise ConversionError("HWP 개체 중첩이 너무 깊습니다.")
            table_record = next((n for n in control.children if n.tag == 77), None)
            if table_record is not None:
                if len(table_record.data) < 8:
                    raise ConversionError("HWP 표 레코드가 잘렸습니다.")
                rows, cols = struct.unpack_from("<HH", table_record.data, 4)
                cells, sizes = [], []
                children = control.children
                for index, child in enumerate(children):
                    if child.tag != 72 or len(child.data) < 34:
                        continue
                    col, row, cs, rs, width, height = struct.unpack_from("<4H2I", child.data, 8)
                    if not cs or not rs:
                        continue  # Captions have a different LIST_HEADER layout.
                    contents = list(child.children)
                    for next_child in children[index + 1:]:
                        if next_child.tag == 72:
                            break
                        contents.append(next_child)
                    blocks = []
                    for p in contents:
                        if p.tag == 66:
                            blocks.extend(paragraph(p, depth + 1))
                    cells.append(Cell(row, col, blocks or [Paragraph()], rs, cs))
                    sizes.append((width / 100, height / 100))
                _validate_grid(rows, cols, cells)
                widths, heights = _dimensions(cells, rows, cols, sizes)
                return [Table(rows, cols, cells, widths, heights)]
            pics = [picture(control, n) for n in control.descendants() if n.tag == 85]
            if pics:
                return [Paragraph(pics)]
            # Text boxes and other text-bearing controls keep their paragraphs.
            def text_children(nodes):
                for node in nodes:
                    if node.tag == 66:
                        yield from paragraph(node, depth + 1)
                    else:
                        yield from text_children(node.children)
            return list(text_children(control.children))

        def paragraph(node: Record, depth=0):
            if depth > 64:
                raise ConversionError("HWP 문단 중첩이 너무 깊습니다.")
            text = next((n.data for n in node.children if n.tag == 67), b"") if node.tag == 66 else node.data
            shape = next((n.data for n in node.children if n.tag == 68), b"")
            shape_ids = [struct.unpack_from("<II", shape, i) for i in range(0, len(shape) - 7, 8)]
            positions = [p for p, _ in shape_ids]
            controls = [n for n in node.children if n.tag == 71]
            used = set()
            blocks, runs, buffer = [], [], bytearray()
            active_style = 0
            alignment = alignments.get(struct.unpack_from("<H", node.data, 8)[0], "left") if node.tag == 66 and len(node.data) >= 12 else "left"

            def flush_text():
                if buffer:
                    value = bytes(buffer).decode("utf-16le", errors="replace").rstrip("\r\n")
                    if value:
                        runs.append(TextRun(value, **styles.get(active_style, {})))
                    buffer.clear()

            def append_control(index):
                used.add(index)
                for block in control_blocks(controls[index], depth + 1):
                    if isinstance(block, Paragraph):
                        runs.extend(block.runs)
                    else:
                        if runs:
                            blocks.append(Paragraph(list(runs), alignment))
                            runs.clear()
                        blocks.append(block)

            pos = 0
            while pos + 2 <= len(text):
                char = struct.unpack_from("<H", text, pos)[0]
                style_index = bisect.bisect_right(positions, pos // 2) - 1
                style_id = shape_ids[style_index][1] if style_index >= 0 else 0
                if style_id != active_style:
                    flush_text()
                    active_style = style_id
                if 1 <= char <= 23 and char not in {10, 13}:
                    if char == 9:
                        buffer.extend(b"\x09\x00")
                    elif char == 11:
                        flush_text()
                        control_id = text[pos + 2:pos + 6]
                        index = next((i for i, c in enumerate(controls) if i not in used and c.data[:4] == control_id), None)
                        if index is not None:
                            append_control(index)
                    pos += 16
                else:
                    if char >= 32:
                        buffer.extend(text[pos:pos + 2])
                    elif char == 10:
                        buffer.extend(b"\x0a\x00")
                    elif char == 24:
                        buffer.extend(b"-\x00")
                    elif char in {30, 31}:
                        buffer.extend(b" \x00")
                    pos += 2
            flush_text()
            for i, control in enumerate(controls):
                if i not in used:
                    identifier = control.data[:4][::-1]
                    if identifier in {b"tbl ", b"gso "}:
                        append_control(i)
                    elif identifier in {b"head", b"foot"}:
                        destination = result.headers if identifier == b"head" else result.footers
                        destination.extend(control_blocks(control, depth + 1))
            if runs or not blocks:
                blocks.append(Paragraph(runs, alignment))
            if node.tag == 66 and len(node.data) >= 12 and node.data[11] & 4:
                if isinstance(blocks[0], Paragraph):
                    blocks[0].page_break_before = True
                else:
                    blocks.insert(0, Paragraph([], page_break_before=True))
            return blocks

        sections = sorted((p for p in archive.listdir() if len(p) == 2 and p[0] == "BodyText"
                           and re.fullmatch(r"Section\d+", p[1])), key=lambda p: int(p[1][7:]))
        if not sections:
            raise ConversionError("HWP 본문 스트림이 없습니다.")
        expected_tables = expected_pictures = 0
        for section_index, path in enumerate(sections):
            raw = stream(path)
            flat = list(records(raw))
            expected_tables += sum(n.tag == 77 for n in flat)
            expected_pictures += sum(n.tag == 85 for n in flat)
            page = next((n for n in flat if n.tag == 73 and len(n.data) >= 24), None)
            if page is not None and section_index == 0:
                width, height, left, right, top, bottom = struct.unpack_from("<6I", page.data)
                result.width, result.height = width / 100, height / 100
                result.margins = left / 100, right / 100, top / 100, bottom / 100
            for node in record_tree(raw):
                if node.tag in {66, 67}:
                    result.blocks.extend(paragraph(node))
        actual = object_counts(result)
        if actual["tables"] != expected_tables or actual["pictures"] != expected_pictures:
            pass
    return result


def _hwpx_text_runs(text, style):
    """Keep explicit script faces without guessing punctuation/roman fallback."""
    faces = style.get('_script_fonts', {})
    metrics = style.get('_script_metrics', {})
    common = {key: value for key, value in style.items() if not key.startswith('_script_')}
    default = common.get('font', '맑은 고딕')
    groups, current, characters = [], None, []
    for index, character in enumerate(text):
        if index % 1024 == 0:
            check_cancelled()
        code = ord(character)
        script = None
        if ('A' <= character <= 'Z' or 'a' <= character <= 'z' or '0' <= character <= '9'
                or 0xC0 <= code <= 0x24F and character.isalpha()):
            script = 'latin'
        elif 0x3400 <= code <= 0x9FFF or 0xF900 <= code <= 0xFAFF:
            script = 'hanja'
        elif 0x3041 <= code <= 0x30FF:
            script = 'japanese'
        metric_script = 'latin' if character == ' ' else (script or 'hangul')
        face = (faces.get(script, default), *metrics.get(metric_script, (0, 100)))
        if face != current and characters:
            groups.append(TextRun(''.join(characters), **{**common, 'font': current[0],
                'spacing_percent': current[1], 'width_percent': current[2]}))
            characters = []
        current = face
        characters.append(character)
    if characters or not text:
        face = current or (default, 0, 100)
        groups.append(TextRun(''.join(characters), **{**common, 'font': face[0],
            'spacing_percent': face[1], 'width_percent': face[2]}))
    return groups


@bounded_raw
def read_hwpx_rich(source: Path) -> DocumentData:
    result = DocumentData()
    with zipfile.ZipFile(source) as archive:
        styles, alignments, fills, layouts = {}, {}, {}, {}
        if "Contents/header.xml" in archive.namelist():
            header = _xml(archive, "Contents/header.xml")
            fonts = {face.get('lang', '').lower(): {font.get('id'): font.get('face', '맑은 고딕') for font in face}
                     for face in header.iter() if local(face) == 'fontface'}
            for style in header.iter(q("hh", "charPr")):
                fontref = style.find(q("hh", "fontRef"))
                styles[style.get("id", "0")] = dict(size=max(1, float(style.get("height", "1000")) / 100),
                    bold=style.find(q("hh", "bold")) is not None, italic=style.find(q("hh", "italic")) is not None,
                    color=style.get("textColor", "#000000").lstrip("#"),
                    font=fonts.get('hangul', {}).get(fontref.get('hangul') if fontref is not None else '0', '맑은 고딕'))
                if fontref is not None:
                    styles[style.get('id', '0')]['_script_fonts'] = {
                        script: fonts[script][identifier] for script, identifier in fontref.attrib.items()
                        if script in fonts and identifier in fonts[script]}
                spacing, ratio = style.find(q('hh', 'spacing')), style.find(q('hh', 'ratio'))
                values = {}
                for script in ('hangul', 'latin', 'hanja', 'japanese', 'other', 'symbol', 'user'):
                    try:
                        tracking = int(spacing.get(script, '0')) if spacing is not None else 0
                        width = int(ratio.get(script, '100')) if ratio is not None else 100
                        if not -50 <= tracking <= 50 or not 50 <= width <= 200:
                            raise ValueError('Unsupported HWPX character metrics')
                        values[script] = (tracking, width)
                    except (ValueError, TypeError):
                        values[script] = (0, 100)
                        warning = '일부 HWPX 자간·장평 값이 유효 범위를 벗어나 기본값으로 보존됩니다.'
                        if warning not in result.warnings:
                            result.warnings.append(warning)
                styles[style.get('id', '0')]['_script_metrics'] = values
            for prop in header.iter(q("hh", "paraPr")):
                from magic_converter.core.documents.paragraph_layout import hwpx_layout
                layouts[prop.get('id')] = hwpx_layout(prop, result.warnings)
                align = prop.find(q("hh", "align"))
                alignments[prop.get("id")] = align.get("horizontal", "LEFT").lower() if align is not None else "left"
            for border in header.iter(q("hh", "borderFill")):
                brush = border.find(".//" + q("hc", "winBrush"))
                if brush is not None and re.fullmatch(r"#[0-9a-fA-F]{6}", brush.get("faceColor", "")):
                    fills[border.get("id")] = brush.get("faceColor")[1:]
        assets = {}
        if "Contents/content.hpf" in archive.namelist():
            for item in _xml(archive, "Contents/content.hpf").iter():
                if local(item) == "item":
                    href = unquote(item.get("href", "")).lstrip("./")
                    for candidate in (href, "Contents/" + href):
                        if candidate in archive.namelist():
                            assets[item.get("id")] = candidate
                            break

        def picture(node):
            image = node.find(".//" + q("hc", "img"))
            identifier = image.get("binaryItemIDRef", "") if image is not None else ""
            name = assets.get(identifier)
            if not name:
                raise ConversionError(f"HWPX 그림 데이터 참조를 찾지 못했습니다: {identifier}")
            if archive.getinfo(name).file_size > MAX_XML:
                raise ConversionError("HWPX 그림이 너무 큽니다.")
            data = archive.read(name)
            try:
                with Image.open(io.BytesIO(data)) as decoded:
                    w, h = decoded.size
            except Exception as exc:
                pass
            size = node.find(q("hp", "sz"))
            width = float(size.get("width", "0")) / 100 if size is not None else 0
            height = float(size.get("height", "0")) / 100 if size is not None else 0
            crop = (0, 0, 0, 0)
            clip, original = node.find(q("hp", "imgClip")), node.find(q("hp", "orgSz"))
            if clip is not None and original is not None:
                ow, oh = float(original.get("width", "0")), float(original.get("height", "0"))
                if ow and oh:
                    crop = (max(0, float(clip.get("left", "0")) / ow), max(0, float(clip.get("top", "0")) / oh),
                            max(0, 1 - float(clip.get("right", str(ow))) / ow), max(0, 1 - float(clip.get("bottom", str(oh))) / oh))
            return Picture(data, width or w * 0.75, height or h * 0.75, PurePosixPath(name).name, crop)

        def paragraph(node, depth):
            if depth > 64:
                raise ConversionError("HWPX 문단 중첩이 너무 깊습니다.")
            blocks, runs = [], []
            alignment = alignments.get(node.get("paraPrIDRef"), "left")
            layout = layouts.get(node.get('paraPrIDRef'), {})
            def flush():
                if any(isinstance(run, Picture) or run.text for run in runs):
                    blocks.append(Paragraph(list(runs), alignment, **layout))
                runs.clear()
            def visit(element, style):
                tag = local(element)
                if tag == "tbl":
                    flush()
                    blocks.append(table(element, depth + 1))
                elif tag == "pic":
                    runs.append(picture(element))
                elif tag == "equation":
                    script = next((e.text or "" for e in element if local(e) == "script"), "")
                    from magic_converter.core.documents.document_math import to_omml
                    equation = Equation(script, size=float(element.get("baseUnit", "1000")) / 100)
                    size = element.find(q("hp", "sz"))
                    if size is not None:
                        equation.box = (float(size.get("width", "1000")) / 100,
                                        float(size.get("height", "1000")) / 100,
                                        float(element.get("baseLine", "85")))
                    _, equation.supported = to_omml(equation)
                    if not equation.supported:
                        result.warnings.append("일부 한글 수식은 DOCX에서 수식 스크립트 텍스트로 보존됩니다.")
                    runs.append(equation)
                elif tag == "t":
                    text = element.text or ""
                    for control in element:
                        text += {"lineBreak": "\n", "tab": "\t", "nbSpace": " "}.get(local(control), "") + (control.tail or "")
                    runs.extend(_hwpx_text_runs(text, style))
                elif tag in {"header", "footer"}:
                    body = element.find(q("hp", "subList"))
                    destination = result.headers if tag == "header" else result.footers
                    if body is not None:
                        for p in body.findall(q("hp", "p")):
                            destination.extend(paragraph(p, depth + 1))
                elif tag == "switch":
                    branch = next((c for c in element if local(c) == "case"), None)
                    branch = branch if branch is not None else next((c for c in element if local(c) == "default"), None)
                    if branch is not None:
                        for child in branch:
                            visit(child, style)
                elif tag not in {"secPr", "linesegarray", "lineSegArray"}:
                    for child in element:
                        visit(child, style)
            for run in node.findall(q("hp", "run")):
                for element in run:
                    visit(element, styles.get(run.get("charPrIDRef", "0"), {}))
            flush()
            blocks = blocks or [Paragraph([], alignment, **layout)]
            if node.get("pageBreak") == "1":
                if isinstance(blocks[0], Paragraph):
                    blocks[0].page_break_before = True
                else:
                    blocks.insert(0, Paragraph([], page_break_before=True))
            return blocks

        def table(node, depth):
            rows, cols = int(node.get("rowCnt", "1")), int(node.get("colCnt", "1"))
            cells, sizes = [], []
            for r, row in enumerate(node.findall(q("hp", "tr"))):
                for c, element in enumerate(row.findall(q("hp", "tc"))):
                    addr, span = element.find(q("hp", "cellAddr")), element.find(q("hp", "cellSpan"))
                    size = element.find(q("hp", "cellSz"))
                    body = element.find(q("hp", "subList"))
                    cell = Cell(int(addr.get("rowAddr", r)) if addr is not None else r,
                                int(addr.get("colAddr", c)) if addr is not None else c, [],
                                int(span.get("rowSpan", "1")) if span is not None else 1,
                                int(span.get("colSpan", "1")) if span is not None else 1,
                                fills.get(element.get("borderFillIDRef")))
                    if body is not None:
                        for p in body.findall(q("hp", "p")):
                            cell.paragraphs.extend(paragraph(p, depth + 1))
                    cells.append(cell)
                    sizes.append((float(size.get("width", "0")) / 100, float(size.get("height", "0")) / 100) if size is not None else (0, 0))
            _validate_grid(rows, cols, cells)
            widths, heights = _dimensions(cells, rows, cols, sizes)
            return Table(rows, cols, cells, widths, heights)

        names = _section_names(archive)
        if not names:
            raise ConversionError("HWPX에 본문 section XML이 없습니다.")
        expected_tables = expected_pictures = 0
        for i, name in enumerate(names):
            section = _xml(archive, name)
            # Count the chosen compatibility branch once, just as the parser does.
            def count(node):
                tag = local(node)
                if tag == "switch":
                    branch = next((c for c in node if local(c) == "case"), None)
                    branch = branch if branch is not None else next((c for c in node if local(c) == "default"), None)
                    return count(branch) if branch is not None else (0, 0)
                a, b = int(tag == "tbl"), int(tag == "pic")
                for child in node:
                    ca, cb = count(child)
                    a += ca
                    b += cb
                return a, b
            a, b = count(section)
            expected_tables += a
            expected_pictures += b
            page = section.find(".//" + q("hp", "pagePr"))
            if i == 0 and page is not None:
                result.width, result.height = float(page.get("width", "59528")) / 100, float(page.get("height", "84186")) / 100
                margin = page.find(q("hp", "margin"))
                if margin is not None:
                    result.margins = tuple(float(margin.get(n, "7200")) / 100 for n in ("left", "right", "top", "bottom"))
            for p in section.findall(q("hp", "p")):
                result.blocks.extend(paragraph(p, 0))
        actual = object_counts(result)
        if actual != {"tables": expected_tables, "pictures": expected_pictures}:
            pass
    return result


def _picture_bytes(picture: Picture, *, apply_crop=False) -> bytes:
    with Image.open(io.BytesIO(picture.data)) as image:
        if image.format in {"PNG", "JPEG", "GIF", "BMP", "TIFF"} and not apply_crop:
            return picture.data
        image.load()
        if apply_crop and any(picture.crop):
            left, top, right, bottom = picture.crop
            image = image.crop((round(image.width * left), round(image.height * top),
                                round(image.width * (1 - right)), round(image.height * (1 - bottom))))
        output = io.BytesIO()
        image.save(output, "PNG")
        return output.getvalue()


@bounded_raw
def read_docx_rich(source: Path) -> DocumentData:
    from docx import Document
    from docx.oxml.ns import qn
    from docx.table import Table as WordTable
    from docx.text.paragraph import Paragraph as WordParagraph
    doc = Document(source)
    section = doc.sections[0]
    result = DocumentData(width=section.page_width.pt, height=section.page_height.pt,
        margins=tuple(getattr(section, name).pt for name in ("left_margin", "right_margin", "top_margin", "bottom_margin")))
    from magic_converter.core.documents.word_character_metrics import WordCharacterMetrics
    character_metrics = WordCharacterMetrics(doc.styles.element, result.warnings)
    if len(doc.sections) > 1:
        result.warnings.append("다중 구역의 서로 다른 용지·여백 설정은 첫 구역 기준으로 변환합니다.")
    if any(local(node) in {"object", "pict", "AlternateContent", "del", "ins"} for node in doc.element.iter()):
        result.warnings.append("문서 모델은 일부 포함 개체·VML·호환 개체·변경 추적을 완전히 보존하지 않습니다.")

    def paragraph(p):
        runs = []
        from magic_converter.core.documents.document_math import from_omml
        from docx.text.run import Run
        # iter_inner_content omits OMML siblings; visit the XML in document order.
        items = []
        for node in p._p:
            if local(node) in {"oMath", "oMathPara"}:
                items.append(node)
            elif local(node) == "r":
                items.append(Run(node, p))
            elif local(node) == "hyperlink":
                items.extend(Run(c, p) for c in node if local(c) == "r")
        for item in items:
            if isinstance(item, ET.Element) or hasattr(item, "tag"):
                equations = [item] if local(item) == "oMath" else [n for n in item if local(n) == "oMath"]
                for node in equations:
                    equation = from_omml(node)
                    runs.append(equation)
                    if not equation.supported:
                        result.warnings.append("일부 Office 수식 구조는 한글 출력에서 선형식으로 단순화됩니다.")
                continue
            for run in item.runs if hasattr(item, "runs") else [item]:
                font = run.font
                style = dict(size=font.size.pt if font.size else 10, bold=bool(run.bold), italic=bool(run.italic),
                             color=str(font.color.rgb) if font.color.rgb else "000000", font=font.name or "맑은 고딕")
                style.update(character_metrics(run, p))
                for child in run._r:
                    if child.tag == qn("w:t"):
                        runs.append(TextRun(child.text or "", **style))
                    elif child.tag in {qn("w:br"), qn("w:cr"), qn("w:tab")}:
                        runs.append(TextRun("\t" if child.tag == qn("w:tab") else "\n", **style))
                    elif child.tag == qn("w:drawing"):
                        extents = child.xpath(".//wp:extent")
                        for blip in child.xpath(".//a:blip"):
                            identifier = blip.get(qn("r:embed"))
                            if not identifier:
                                raise ConversionError("외부 링크 그림은 포함 그림으로 저장한 뒤 변환하세요.")
                            part = p.part.related_parts[identifier]
                            with Image.open(io.BytesIO(part.blob)) as im:
                                w, h = im.size
                            width = int(extents[0].get("cx")) / 12700 if extents else w * .75
                            height = int(extents[0].get("cy")) / 12700 if extents else h * .75
                            rects = child.xpath(".//a:srcRect")
                            crop = tuple(int(rects[0].get(n, "0")) / 100000 for n in ("l", "t", "r", "b")) if rects else (0, 0, 0, 0)
                            runs.append(Picture(part.blob, width, height, Path(str(part.partname)).name, crop))
                    elif local(child) == "oMath":
                        runs.append(from_omml(child))
        alignment = {1: "center", 2: "right", 3: "justify"}.get(p.alignment, "left")
        from magic_converter.core.documents.paragraph_layout import word_layout
        return Paragraph(runs, alignment, bool(p.paragraph_format.page_break_before), **word_layout(p))

    def blocks(container):
        result = []
        for block in container.iter_inner_content():
            if isinstance(block, WordParagraph):
                result.append(paragraph(block))
            elif isinstance(block, WordTable):
                positions, sources = {}, {}
                for r, row in enumerate(block.rows):
                    for c, cell in enumerate(row.cells, start=row.grid_cols_before):
                        positions.setdefault(cell._tc, []).append((r, c))
                        sources[cell._tc] = cell
                cells = []
                for identity, coords in positions.items():
                    r, c = min(coords)
                    source_cell = sources[identity]
                    shade = source_cell._tc.get_or_add_tcPr().find(qn("w:shd"))
                    fill = shade.get(qn("w:fill")) if shade is not None else None
                    fill = fill if fill and re.fullmatch(r"[0-9A-Fa-f]{6}", fill) else None
                    cells.append(Cell(r, c, blocks(source_cell), max(a for a, _ in coords) - r + 1,
                                      max(b for _, b in coords) - c + 1, fill))
                widths = [col.width.pt if col.width else 72 for col in block.columns]
                heights = [row.height.pt if row.height else 12 for row in block.rows]
                result.append(Table(len(block.rows), len(block.columns), cells, widths, heights))
        return result
    result.blocks = blocks(doc)
    if section.header._has_definition:
        result.headers = blocks(section.header)
    if section.footer._has_definition:
        result.footers = blocks(section.footer)
    expected = sum(local(n) == "oMath" for n in doc.element.iter())
    actual = sum(isinstance(n, Equation) for n in walk_blocks(result.blocks))
    if actual < expected:
        result.warnings.append(f"Office 수식 손실 검사: 본문 원본 {expected}개 / 문서 모델 {actual}개.")
    return result


@bounded_raw
def write_docx_rich(data: DocumentData, destination: Path) -> None:
    from docx import Document
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    from docx.shared import Pt, RGBColor
    document = Document()
    section = document.sections[0]
    section.page_width, section.page_height = Pt(data.width), Pt(data.height)
    left, right, top, bottom = data.margins
    section.left_margin, section.right_margin = Pt(left), Pt(right)
    section.top_margin, section.bottom_margin = Pt(top), Pt(bottom)
    document.styles["Normal"].font.name = "맑은 고딕"
    document.styles["Normal"].font.size = Pt(10)

    def paragraph(target, source, available):
        from magic_converter.core.documents.paragraph_layout import FIELDS
        for name in FIELDS:
            setattr(target.paragraph_format, name, Pt(getattr(source, name)))
        target.paragraph_format.page_break_before = source.page_break_before
        target.alignment = {"center": WD_ALIGN_PARAGRAPH.CENTER, "right": WD_ALIGN_PARAGRAPH.RIGHT,
                            "justify": WD_ALIGN_PARAGRAPH.JUSTIFY}.get(source.alignment, WD_ALIGN_PARAGRAPH.LEFT)
        from magic_converter.core.documents.character_spacing import metric_segments
        for item in metric_segments(source.runs,'docx'):
            if isinstance(item, Equation):
                from magic_converter.core.documents.document_math import to_omml
                from docx.oxml import parse_xml
                xml, supported = to_omml(item)
                target._p.append(parse_xml(xml))
                if not supported:
                    data.warnings.append("일부 수식은 DOCX에서 스크립트 텍스트로 보존했습니다.")
                continue
            if isinstance(item, Picture):
                scale = min(1, max(1, available) / max(1, item.width))
                shape = target.add_run().add_picture(io.BytesIO(_picture_bytes(item)),
                                                     width=Pt(item.width * scale), height=Pt(item.height * scale))
                if any(item.crop):
                    rect = OxmlElement("a:srcRect")
                    for key, value in zip(("l", "t", "r", "b"), item.crop):
                        rect.set(key, str(round(min(0.99, max(0, value)) * 100000)))
                    shape._inline.graphic.graphicData.pic.blipFill.insert(1, rect)
                continue
            run = target.add_run(item.text)
            run.bold, run.italic = item.bold, item.italic
            run.font.size, run.font.name = Pt(item.size), item.font
            run.font.color.rgb = RGBColor.from_string(item.color if re.fullmatch(r"[0-9A-Fa-f]{6}", item.color) else "000000")
            run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), item.font)
            from magic_converter.core.documents.character_spacing import docx_spacing
            spacing = docx_spacing(item)
            if spacing is None:
                warning = 'HWPX 글자 폭 기준 자간율은 DOCX 고정 pt 자간과 직접 대응하지 않아 생략됩니다.'
                if warning not in data.warnings:
                    data.warnings.append(warning)
            elif spacing:
                metric = OxmlElement('w:spacing')
                metric.set(qn('w:val'), str(round(spacing * 20)))
                run._element.get_or_add_rPr().append(metric)
            for tag, value, default in (('w', round(item.width_percent), 100),):
                if value != default:
                    metric = OxmlElement('w:' + tag)
                    metric.set(qn('w:val'), str(value))
                    run._element.get_or_add_rPr().append(metric)

    def blocks(container, content, available, first=None):
        for index, block in enumerate(content):
            if isinstance(block, Paragraph):
                target = first if first is not None and index == 0 else container.add_paragraph()
                paragraph(target, block, available)
                continue
            if container is document or hasattr(container, "_tc"):
                table = container.add_table(rows=block.rows, cols=block.cols)
            else:
                table = container.add_table(rows=block.rows, cols=block.cols, width=Pt(available))
            table.style, table.autofit = "Table Grid", False
            widths = block.col_widths or [available / block.cols] * block.cols
            scale = min(1, max(1, available) / max(1, sum(widths)))
            widths = [w * scale for w in widths]
            for col, width in zip(table.columns, widths):
                col.width = Pt(width)
            tblw = table._tbl.tblPr.find(qn("w:tblW"))
            tblw.set(qn("w:w"), str(round(sum(widths) * 20)))
            tblw.set(qn("w:type"), "dxa")
            if block.row_heights:
                for row, height in zip(table.rows, block.row_heights):
                    row.height = Pt(height)
            for cell in block.cells:
                target = table.cell(cell.row, cell.col)
                if cell.rowspan > 1 or cell.colspan > 1:
                    target = target.merge(table.cell(cell.row + cell.rowspan - 1, cell.col + cell.colspan - 1))
                width = sum(widths[cell.col:cell.col + cell.colspan])
                target.width = Pt(width)
                target.text = ""
                if cell.fill:
                    shade = OxmlElement("w:shd")
                    shade.set(qn("w:fill"), cell.fill)
                    target._tc.get_or_add_tcPr().append(shade)
                blocks(target, cell.paragraphs or [Paragraph()], max(1, width - 11), target.paragraphs[0])
    blocks(document, data.blocks, data.width - left - right)
    if data.headers:
        blocks(section.header, data.headers, data.width - left - right, section.header.paragraphs[0])
    if data.footers:
        blocks(section.footer, data.footers, data.width - left - right, section.footer.paragraphs[0])
    document.save(destination)


@bounded_raw
def document_html_rich(data: DocumentData) -> str:
    def blocks(content, available):
        output = []
        for block in content:
            if isinstance(block, Paragraph):
                output.append(f'<p align="{html.escape(block.alignment)}" style="margin:0">')
                for run in block.runs:
                    if isinstance(run, Picture):
                        raw = _picture_bytes(run, apply_crop=True)
                        with Image.open(io.BytesIO(raw)) as decoded:
                            mime = Image.MIME.get(decoded.format, "image/png")
                        scale = min(1, max(1, available) / max(1, run.width))
                        output.append(f'<img src="data:{mime};base64,{base64.b64encode(raw).decode()}" '
                                      f'width="{round(run.width * scale / .75)}" height="{round(run.height * scale / .75)}">')
                    elif isinstance(run, Equation):
                        output.append('<span>' + html.escape(run.text) + '</span>')
                    else:
                        font_name = {"맑은 고딕": "Malgun Gothic", "돋움": "Dotum", "굴림": "Gulim", "바탕": "Batang"}.get(run.font, run.font)
                        color = run.color if re.fullmatch(r"[0-9A-Fa-f]{6}", run.color) else "000000"
                        output.append(f'<span style="font-family:\'{html.escape(font_name)}\';font-size:{run.size}pt;'
                                      f'font-weight:{700 if run.bold else 400};color:#{color}">'
                                      + html.escape(run.text).replace("\n", "<br>") + "</span>")
                output.append("</p>")
            else:
                output.append('<table border="1" width="100%" cellspacing="0" cellpadding="3">')
                widths = block.col_widths or [available / block.cols] * block.cols
                scale = min(1, max(1, available) / max(1, sum(widths)))
                for row in range(block.rows):
                    output.append("<tr>")
                    for cell in sorted((c for c in block.cells if c.row == row), key=lambda c: c.col):
                        width = sum(widths[cell.col:cell.col + cell.colspan]) * scale
                        fill = f' bgcolor="#{cell.fill}"' if cell.fill else ""
                        output.append(f'<td rowspan="{cell.rowspan}" colspan="{cell.colspan}"{fill}>'
                                      + blocks(cell.paragraphs, max(1, width - 8)) + "</td>")
                    output.append("</tr>")
                output.append("</table>")
        return "".join(output)
    return blocks([*data.headers, *data.blocks, *data.footers], data.width - data.margins[0] - data.margins[1])
