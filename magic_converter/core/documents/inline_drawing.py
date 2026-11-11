"""Preserve inline Word drawings that contain a single horizontal line."""
from __future__ import annotations

import re
from xml.etree import ElementTree as ET

from magic_converter.core.documents.hwp_documents import InlineLine, q

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_WP = "{http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing}"
_WPG = "{http://schemas.microsoft.com/office/word/2010/wordprocessingGroup}"
_WPS = "{http://schemas.microsoft.com/office/word/2010/wordprocessingShape}"
_EMU_PER_POINT = 12700


def _pair(element, x, y):
    if element is None:
        raise ValueError("Drawing coordinates are missing")
    return int(element.get(x, "")), int(element.get(y, ""))


def _plain_transform(transform):
    return transform is not None and all(transform.get(key, "0") in {"0", "false"} for key in ("rot", "flipH", "flipV"))


def read_word_line(drawing):
    inline = drawing.find(_WP + "inline")
    if inline is None:
        return None
    graphic = inline.find(_A + "graphic/" + _A + "graphicData")
    if graphic is None or len(graphic) != 1:
        return None
    group = graphic[0]
    if group.tag != _WPG + "wgp":
        return None
    group_properties = group.find(_WPG + "grpSpPr")
    shapes = [child for child in group if child.tag not in {_WPG + "cNvGrpSpPr", _WPG + "grpSpPr"}]
    if (len(shapes) != 1 or shapes[0].tag != _WPS + "wsp" or group_properties is None
            or [child.tag for child in group_properties] != [_A + "xfrm"]):
        return None
    shape = shapes[0]
    props = shape.find(_WPS + "spPr")
    if props is None or shape.find(_WPS + "txbx") is not None:
        return None
    group_transform = group_properties.find(_A + "xfrm")
    transform = props.find(_A + "xfrm")
    if not _plain_transform(group_transform) or not _plain_transform(transform):
        return None
    if any(child.tag not in {_A + name for name in ("xfrm", "custGeom", "ln", "noFill")} for child in props):
        return None
    paths = props.findall(_A + "custGeom/" + _A + "pathLst/" + _A + "path")
    if len(paths) != 1 or [node.tag for node in paths[0]] != [_A + "moveTo", _A + "lnTo"]:
        return None
    outline = props.find(_A + "ln")
    if outline is None or any(node.tag not in {_A + "solidFill", _A + "prstDash"} for node in outline):
        return None
    color = outline.find(_A + "solidFill/" + _A + "srgbClr")
    dash = outline.find(_A + "prstDash")
    if color is None or len(color) or not re.fullmatch(r"[0-9a-fA-F]{6}", color.get("val", "")):
        return None
    if dash is not None and dash.get("val") != "solid":
        return None
    try:
        width, height = _pair(inline.find(_WP + "extent"), "cx", "cy")
        origin_x, origin_y = _pair(group_transform.find(_A + "chOff"), "x", "y")
        group_width, group_height = _pair(group_transform.find(_A + "chExt"), "cx", "cy")
        offset_x, offset_y = _pair(transform.find(_A + "off"), "x", "y")
        shape_width, shape_height = _pair(transform.find(_A + "ext"), "cx", "cy")
        start, end = [_pair(node.find(_A + "pt"), "x", "y") for node in paths[0]]
        path_width = int(paths[0].get("w", str(shape_width)))
        path_height = int(paths[0].get("h", str(shape_height)))
        stroke = int(outline.get("w", "0"))
        if min(width, height, group_width, group_height, shape_width, path_width, stroke) <= 0:
            return None
        if max(width, height, group_width, group_height, shape_width, shape_height, stroke) > 100_000_000:
            return None
        if start[1] != end[1] or not 0 <= min(start[0], end[0]) < max(start[0], end[0]) <= path_width:
            return None
        if not 0 <= start[1] <= path_height or (path_height == 0 and start[1]):
            return None
        xs = [(offset_x + shape_width * x / path_width - origin_x) * width / group_width for x, _ in (start, end)]
        y = (offset_y + (shape_height * start[1] / path_height if path_height else 0) - origin_y) * height / group_height
        stroke *= height / group_height
        if not (0 <= min(xs) < max(xs) <= width and abs(xs[1] - xs[0]) >= 127
                and 0 <= y <= height and 0 < stroke <= height * 2):
            return None
        return InlineLine(*(value / _EMU_PER_POINT for value in (width, height, *sorted(xs), y, stroke)), color.get("val").upper())
    except (TypeError, ValueError, OverflowError):
        return None


def read_hwpx_line(node):
    size = node.find(q("hp", "sz"))
    position = node.find(q("hp", "pos"))
    outline = node.find(q("hp", "lineShape"))
    if position is None or position.get("treatAsChar") != "1" or outline is None or outline.get("style") != "SOLID":
        return None
    if any(outline.get(key, "NONE") != "NONE" for key in ("headStyle", "tailStyle")):
        return None
    if node.get("href") or any(node.get(key, "0") != "0" for key in ("isReverseHV", "groupLevel")):
        return None
    color = outline.get("color", "").lstrip("#")
    if not re.fullmatch(r"[0-9a-fA-F]{6}", color):
        return None
    try:
        width, height = _pair(size, "width", "height")
        if any(_pair(node.find(q("hp", tag)), "width", "height") != (width, height) for tag in ("orgSz", "curSz")):
            return None
        if _pair(node.find(q("hp", "offset")), "x", "y") != (0, 0):
            return None
        if _pair(node.find(q("hp", "flip")), "horizontal", "vertical") != (0, 0):
            return None
        rotation = node.find(q("hp", "rotationInfo"))
        if rotation is None or rotation.get("angle") != "0":
            return None
        rendering = node.find(q("hp", "renderingInfo"))
        if rendering is None or any(
            tuple(float(matrix.get(f"e{i}", "")) for i in range(1, 7)) != (1, 0, 0, 0, 1, 0)
            for matrix in rendering
        ):
            return None
        start, end = [_pair(node.find(q("hc", point)), "x", "y") for point in ("startPt", "endPt")]
        stroke = int(outline.get("width", "0"))
        if not (0 <= min(start[0], end[0]) < max(start[0], end[0]) <= width <= 800_000
                and 0 <= start[1] == end[1] <= height <= 800_000 and 0 < stroke <= height * 2):
            return None
        return InlineLine(*(value / 100 for value in (width, height, *sorted((start[0], end[0])), start[1], stroke)), color.upper())
    except (TypeError, ValueError, OverflowError):
        return None


def write_hwpx_line(parent, item, available, identity):
    def sub(parent, tag, **attrs):
        return ET.SubElement(parent, q("hp", tag), {key: str(value) for key, value in attrs.items()})

    scale = min(1, available / max(1, item.width * 100))
    width, height, left, right, y, stroke = [
        round(value * scale * 100) for value in (item.width, item.height, item.left, item.right, item.y, item.stroke)
    ]
    width, height, stroke = max(1, width), max(1, height), max(1, stroke)
    line = sub(parent, "line", id=identity, instid=identity, zOrder=0, numberingType="NONE", textWrap="TOP_AND_BOTTOM",
               textFlow="BOTH_SIDES", lock=0, dropcapstyle="None", href="", groupLevel=0, isReverseHV=0)
    sub(line, "offset", x=0, y=0)
    for tag in ("orgSz", "curSz"):
        sub(line, tag, width=width, height=height)
    sub(line, "flip", horizontal=0, vertical=0)
    sub(line, "rotationInfo", angle=0, centerX=width // 2, centerY=height // 2, rotateimage=1)
    rendering = sub(line, "renderingInfo")
    for tag in ("transMatrix", "scaMatrix", "rotMatrix"):
        ET.SubElement(rendering, q("hc", tag), e1="1", e2="0", e3="0", e4="0", e5="1", e6="0")
    sub(line, "lineShape", color="#" + item.color, width=stroke, style="SOLID", endCap="FLAT",
        headStyle="NONE", tailStyle="NONE", headfill=0, tailfill=0, headSz="SMALL_SMALL", tailSz="SMALL_SMALL",
        outlineStyle="NORMAL", alpha=0)
    ET.SubElement(line, q("hc", "startPt"), x=str(left), y=str(y))
    ET.SubElement(line, q("hc", "endPt"), x=str(right), y=str(y))
    sub(line, "sz", width=width, height=height, widthRelTo="ABSOLUTE", heightRelTo="ABSOLUTE", protect=0)
    sub(line, "pos", treatAsChar=1, affectLSpacing=0, flowWithText=1, allowOverlap=0, holdAnchorAndSO=0,
        vertRelTo="PARA", horzRelTo="COLUMN", vertAlign="TOP", horzAlign="LEFT", vertOffset=0, horzOffset=0)
    sub(line, "outMargin", left=0, right=0, top=0, bottom=0)
    sub(line, "shapeComment").text = "Line"

def write_word_line(run, item, available, identity):
    from docx.oxml import OxmlElement
    from lxml import etree

    def sub(parent, tag, **attrs):
        return etree.SubElement(parent, tag, {key: str(value) for key, value in attrs.items()})

    scale = min(1, max(1, available) / item.width)
    width, height, left, right, y, stroke = (
        round(value * scale * _EMU_PER_POINT)
        for value in (item.width, item.height, item.left, item.right, item.y, item.stroke)
    )
    drawing = OxmlElement("w:drawing")
    run._r.append(drawing)
    inline = sub(drawing, _WP + "inline", distT=0, distB=0, distL=0, distR=0)
    sub(inline, _WP + "extent", cx=width, cy=height)
    sub(inline, _WP + "docPr", id=identity, name="Line")
    graphic = sub(inline, _A + "graphic")
    content = sub(graphic, _A + "graphicData", uri=_WPG[1:-1])
    group = sub(content, _WPG + "wgp")
    sub(group, _WPG + "cNvGrpSpPr")
    transform = sub(sub(group, _WPG + "grpSpPr"), _A + "xfrm")
    sub(transform, _A + "off", x=0, y=0)
    sub(transform, _A + "ext", cx=width, cy=height)
    sub(transform, _A + "chOff", x=0, y=0)
    sub(transform, _A + "chExt", cx=width, cy=height)
    shape = sub(group, _WPS + "wsp")
    sub(shape, _WPS + "cNvPr", id=identity + 1, name="Line segment")
    sub(shape, _WPS + "cNvSpPr")
    props = sub(shape, _WPS + "spPr")
    transform = sub(props, _A + "xfrm")
    sub(transform, _A + "off", x=0, y=0)
    sub(transform, _A + "ext", cx=width, cy=height)
    geometry = sub(props, _A + "custGeom")
    for name in ("avLst", "gdLst", "ahLst", "cxnLst"):
        sub(geometry, _A + name)
    sub(geometry, _A + "rect", l="l", t="t", r="r", b="b")
    path = sub(sub(geometry, _A + "pathLst"), _A + "path", w=width, h=height)
    for tag, x in (("moveTo", left), ("lnTo", right)):
        sub(sub(path, _A + tag), _A + "pt", x=x, y=y)
    line = sub(props, _A + "ln", w=stroke)
    sub(sub(line, _A + "solidFill"), _A + "srgbClr", val=item.color)
    sub(line, _A + "prstDash", val="solid")
    sub(shape, _WPS + "bodyPr")
