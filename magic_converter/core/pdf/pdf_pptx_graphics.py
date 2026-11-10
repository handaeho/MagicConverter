"""Editable DrawingML shapes from PDF vector paths (including cubic curves)."""

from __future__ import annotations

import math
import re

import pymupdf as fitz
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from pptx.oxml.ns import qn
from pptx.oxml.xmlchemy import OxmlElement
from pptx.util import Pt

from magic_converter.core.pdf.pdf_layout import rgb_hex


def path_commands(drawing):
    commands, current, first = [], None, None

    def move(point):
        nonlocal current, first
        if current is None or abs(point - current) > .01:
            commands.append(("M", [point]))
            current, first = point, point

    for item in drawing["items"]:
        if item[0] == "close":
            commands.append(("Z", []))
            current = first = None
        elif item[0] == "re":
            r = fitz.Rect(item[1])
            points = [r.tl, r.tr, r.br, r.bl]
            if len(item) > 2 and item[2] < 0:
                points.reverse()
            commands.extend([("M", [points[0]])] + [("L", [p]) for p in points[1:]] + [("Z", [])])
            current = first = None
        elif item[0] == "qu":
            q = fitz.Quad(item[1])
            commands.extend([("M", [q.ul]), ("L", [q.ur]), ("L", [q.lr]), ("L", [q.ll]), ("Z", [])])
            current = first = None
        elif item[0] == "l":
            a, b = fitz.Point(item[1]), fitz.Point(item[2])
            move(a)
            commands.append(("L", [b]))
            current = b
        elif item[0] == "c":
            points = [fitz.Point(p) for p in item[1:]]
            move(points[0])
            commands.append(("C", points[1:]))
            current = points[-1]
        else:
            return None
    if drawing.get("closePath") and current is not None:
        commands.append(("Z", []))
    return commands


def supported_path(drawing):
    commands = path_commands(drawing)
    if not commands:
        return False
    # DrawingML uses nonzero winding. Multi-contour even-odd fills can have
    # holes/overlaps with different winding, so retain those paths as pixels.
    if drawing.get("fill") is not None and drawing.get("even_odd") and sum(c == "M" for c, _ in commands) > 1:
        return False
    return True


def _alpha(color, opacity):
    if opacity is not None and opacity < .999:
        element = OxmlElement("a:alpha")
        element.set("val", str(round(max(0, min(1, opacity)) * 100000)))
        color.append(element)


def _style(shape, drawing, scale):
    if hasattr(shape, "fill"):
        if drawing.get("fill") is None:
            shape.fill.background()
        else:
            shape.fill.solid()
            shape.fill.fore_color.rgb = RGBColor.from_string(rgb_hex(drawing["fill"]))
            _alpha(shape.fill._xPr.find(qn("a:solidFill"))[0], drawing.get("fill_opacity"))
    if drawing.get("color") is None:
        shape.line.fill.background()
    else:
        shape.line.color.rgb = RGBColor.from_string(rgb_hex(drawing["color"]))
        shape.line.width = Pt(max(.05, drawing.get("width") or .25) * scale)
        line = shape._element.spPr.get_or_add_ln()
        color = line.find(qn("a:solidFill"))[0]
        _alpha(color, drawing.get("stroke_opacity"))
        caps = drawing.get("lineCap") or (0,)
        cap = max(caps) if isinstance(caps, (list, tuple)) else caps
        line.set("cap", {0: "flat", 1: "rnd", 2: "sq"}.get(cap, "flat"))
        join = OxmlElement({0: "a:miter", 1: "a:round", 2: "a:bevel"}.get(round(drawing.get("lineJoin") or 0), "a:miter"))
        if join.tag == qn("a:miter"):
            join.set("lim", str(round(drawing.get("miter_limit", 1) * 100000)))
        line.append(join)
        dash = drawing.get("dashes") or "[] 0"
        match = re.match(r"\[([^]]*)\]\s*([-+\d.]+)", dash)
        if match and match[1].strip():
            lengths = [float(x) for x in match[1].split()]
            # Custom dash offsets are not expressible by DrawingML. Paths
            # requiring a nonzero phase are screened out by the caller.
            if len(lengths) % 2:
                lengths *= 2
            custom = OxmlElement("a:custDash")
            width = max(.05, drawing.get("width") or .25)
            for ink, gap in zip(lengths[::2], lengths[1::2]):
                ds = OxmlElement("a:ds")
                ds.set("d", str(max(1, round(ink / width * 100000))))
                ds.set("sp", str(max(1, round(gap / width * 100000))))
                custom.append(ds)
            line.insert(1, custom)
    # Do not inherit a template's shadow/effects for reconstructed PDF paths.
    shape._element.spPr.append(OxmlElement("a:effectLst"))


def _ellipse(drawing):
    items = drawing["items"]
    if len(items) != 4 or any(i[0] != "c" for i in items):
        return False
    r = fitz.Rect(drawing["rect"])
    cardinal = [fitz.Point(r.x0, (r.y0 + r.y1) / 2), fitz.Point(r.x1, (r.y0 + r.y1) / 2),
                fitz.Point((r.x0 + r.x1) / 2, r.y0), fitz.Point((r.x0 + r.x1) / 2, r.y1)]
    if not all(any(abs(fitz.Point(i[1]) - p) < .05 for p in cardinal) for i in items):
        return False
    # Ellipse quarter-circle cubic controls use kappa = 4*(sqrt(2)-1)/3.
    for item in items:
        a, b, c, d = (fitz.Point(p) for p in item[1:])
        if abs(d - fitz.Point(items[(items.index(item) + 1) % 4][1])) > .05:
            return False
        expected = (abs(d.x - a.x) + abs(d.y - a.y)) * .55228475
        if abs(abs(b - a) + abs(d - c) - expected) > max(.1, expected * .005):
            return False
    return True


def add_path(slide, drawing, scale, offset_x, offset_y):
    r = fitz.Rect(drawing["rect"])
    items = drawing["items"]
    x, y = Pt(offset_x + r.x0 * scale), Pt(offset_y + r.y0 * scale)
    width, height = Pt(max(.001, r.width) * scale), Pt(max(.001, r.height) * scale)
    if len(items) == 1 and items[0][0] == "re":
        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, width, height)
    elif len(items) == 1 and items[0][0] == "l":
        a, b = items[0][1:]
        shape = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Pt(offset_x + a.x * scale),
                                           Pt(offset_y + a.y * scale), Pt(offset_x + b.x * scale), Pt(offset_y + b.y * scale))
    elif _ellipse(drawing):
        shape = slide.shapes.add_shape(MSO_SHAPE.OVAL, x, y, width, height)
    else:
        shape = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, x, y, width, height)
        properties = shape._element.spPr
        properties.remove(properties.find(qn("a:prstGeom")))
        geometry = OxmlElement("a:custGeom")
        for tag in ("avLst", "gdLst", "ahLst", "cxnLst"):
            geometry.append(OxmlElement("a:" + tag))
        text_rect = OxmlElement("a:rect")
        for key, val in (("l", "0"), ("t", "0"), ("r", "w"), ("b", "h")):
            text_rect.set(key, val)
        geometry.append(text_rect)
        paths = OxmlElement("a:pathLst")
        path = OxmlElement("a:path")
        path.set("w", str(max(1, round(r.width * 1000))))
        path.set("h", str(max(1, round(r.height * 1000))))
        for command, points in path_commands(drawing):
            element = OxmlElement({"M": "a:moveTo", "L": "a:lnTo", "C": "a:cubicBezTo", "Z": "a:close"}[command])
            for point in points:
                p = OxmlElement("a:pt")
                p.set("x", str(round((point.x - r.x0) * 1000)))
                p.set("y", str(round((point.y - r.y0) * 1000)))
                element.append(p)
            path.append(element)
        paths.append(path)
        geometry.append(paths)
        properties.insert(1, geometry)
    _style(shape, drawing, scale)
    shape.name = f"PDF shape {drawing['seqno'] + 1}"
    return shape
