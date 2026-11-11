"""Translate the cell formatting shared by Word and HWPX tables."""
import re

from magic_converter.core.documents.hwp_documents import Border, q

EDGES = ("left", "right", "top", "bottom")
HWPX_TO_WORD = {
    "NONE": "nil", "SOLID": "single", "DOT": "dotted", "DASH": "dashed",
    "DASH_DOT": "dotDash", "DASH_DOT_DOT": "dotDotDash", "LONG_DASH": "dashLong",
    "DOUBLE_SLIM": "double", "SLIM_THICK": "thinThickMediumGap",
    "THICK_SLIM": "thickThinMediumGap", "SLIM_THICK_SLIM": "thinThickThinMediumGap",
    "WAVE": "wave", "DOUBLEWAVE": "doubleWave",
}
WORD_TO_HWPX = {word: hwpx for hwpx, word in HWPX_TO_WORD.items()}
MM_WIDTHS = (0.1, 0.12, 0.15, 0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.7, 1, 1.5, 2, 3, 4, 5)


def _color(value):
    return value.upper() if re.fullmatch(r"[0-9a-fA-F]{6}", value) else "000000"


def hwpx_borders(fill):
    borders = []
    for edge in EDGES:
        line = fill.find(q("hh", edge + "Border"))
        if line is None or line.get("type", "NONE") == "NONE":
            borders.append(Border())
            continue
        size = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*mm", line.get("width", ""))
        width = min(12, float(size[1]) * 72 / 25.4) if size else 0.5
        borders.append(Border(HWPX_TO_WORD.get(line.get("type"), "single"),
                              width, _color(line.get("color", "").lstrip("#"))))
    return tuple(borders)


def hwpx_edge_attributes(border):
    style = WORD_TO_HWPX.get(border.style, "single")
    mm = min(MM_WIDTHS, key=lambda value: abs(value - border.width * 25.4 / 72))
    return {"type": style, "width": f"{mm:g} mm", "color": "#" + border.color}


def hwpx_margins(node):
    if node is None:
        return None
    return tuple(max(0, int(node.get(edge, "0"))) / 100 for edge in EDGES)


def word_border(line):
    from docx.oxml.ns import qn

    style = line.get(qn("w:val"), "nil") if line is not None else "nil"
    if style in {"none", "nil"}:
        return Border()
    size = line.get(qn("w:sz"), "")
    width = min(96, max(2, int(size))) / 8 if size.isdecimal() else 0.5
    return Border(style, width, _color(line.get(qn("w:color"), "")))


def word_border_element(tag, borders):
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn

    result = OxmlElement("w:" + tag)
    for i in (2, 0, 3, 1):  # OOXML order: top, left, bottom, right
        edge = borders[i]
        line = OxmlElement("w:" + EDGES[i])
        line.set(qn("w:val"), edge.style)
        if edge.style != "nil":
            line.set(qn("w:sz"), str(min(96, max(2, round(edge.width * 8)))))
            line.set(qn("w:color"), edge.color)
        result.append(line)
    return result


def word_cell_borders(properties, table_borders, row, col, rowspan, colspan, rows, cols):
    from docx.oxml.ns import qn

    local = properties.find(qn("w:tcBorders")) if properties is not None else None
    outside = (col == 0, col + colspan == cols, row == 0, row + rowspan == rows)
    borders = []
    for i, edge in enumerate(EDGES):
        alias = {"left": "start", "right": "end"}.get(edge)
        line = local.find(qn("w:" + edge)) if local is not None else None
        if line is None and local is not None and alias:
            line = local.find(qn("w:" + alias))
        if line is None and table_borders is not None:
            name = edge if outside[i] else ("insideV" if i < 2 else "insideH")
            line = table_borders.find(qn("w:" + name))
            if line is None and outside[i] and alias:
                line = table_borders.find(qn("w:" + alias))
        borders.append(word_border(line))
    return tuple(borders)


def word_cell_margins(properties, table_margins):
    from docx.oxml.ns import qn

    local = properties.find(qn("w:tcMar")) if properties is not None else None
    margins = []
    for edge, fallback in zip(EDGES, (5.4, 5.4, 0, 0)):
        node = None
        for source in (local, table_margins):
            if source is None:
                continue
            node = source.find(qn("w:" + edge))
            if node is None and edge in {"left", "right"}:
                node = source.find(qn("w:" + ("start" if edge == "left" else "end")))
            if node is not None:
                break
        raw = node.get(qn("w:w"), "") if node is not None else ""
        margins.append(int(raw) / 20 if raw.isdecimal() else fallback)
    return tuple(margins)
