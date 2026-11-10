"""Editable common Office Math ↔ HWP equation scripts; never silently drop math.

Original OMML is retained for DOCX→DOCX. Unsupported constructs are explicitly
marked when reducing them to a linear expression for another document format.
"""
from __future__ import annotations

import re
import os
from pathlib import Path
from xml.etree import ElementTree as ET

from magic_converter.core.documents.hwp_documents import Equation, local

M = "http://schemas.openxmlformats.org/officeDocument/2006/math"


def measure(equation: Equation) -> tuple[float, float, float]:
    """Size common math like rhwp, avoiding stretching to the script's length.

    Fraction/root/script proportions reference Filee EquationScript.Measure
    (MIT; attribution in docs/legal/THIRD_PARTY_NOTICES.md). Width uses local font metrics.
    """
    from PIL import ImageFont
    root = ET.fromstring(to_omml(equation)[0])
    fonts = {}

    def text_width(text, size):
        name = "timesi.ttf" if re.search(r"[A-Za-z]", text) else "times.ttf"
        key = (name, round(size * 100))
        if key not in fonts:
            try:
                fonts[key] = ImageFont.truetype(str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name), key[1])
            except OSError:
                fonts[key] = None
        font = fonts[key]
        return font.getlength(text) / 100 if font else len(text) * size * .55

    def box(node, size):
        if node is None:
            return 0, 0, 0
        tag = local(node)
        part = lambda name, scale=1: box(next((c for c in node if local(c) == name), None), size * scale)
        if tag.endswith("Pr"):
            return 0, 0, 0
        if tag == "t":
            return text_width(node.text or "", size), size, size * .8
        if tag == "f":
            n, d = part("num"), part("den")
            top = n[1] + size * .2
            return max(n[0], d[0]) + size * .4, top + d[1] + size * .24, top + size * .27
        if tag == "rad":
            b, degree = part("e"), part("deg", .7)
            return max(degree[0], size * .3) + size * .4 + b[0], b[1] + size * .2, b[2] + size * .1
        if tag in {"sSup", "sSub", "sSubSup"}:
            b, sub, sup = part("e"), part("sub", .7), part("sup", .7)
            # Same single-script positioning as rhwp's equation layout.
            shift = b[2] - sup[1] * .7
            y = max(sup[1] - b[1], 0) if shift >= 0 else -shift
            y = y if sup[1] else 0
            height = max(y + b[1], sup[1], y + b[2] * .4 + sub[1] if sub[1] else 0)
            return b[0] + max(sub[0], sup[0]), height, y + b[2]
        parts = [box(c, size) for c in node]
        ascent = max((b[2] for b in parts), default=0)
        descent = max((b[1] - b[2] for b in parts), default=0)
        return sum(b[0] for b in parts), ascent + descent, ascent

    width, height, baseline = box(root, max(1, equation.size))
    return max(1, width), max(1, height), min(100, max(0, baseline * 100 / max(1, height)))


def from_omml(node) -> Equation:
    supported = True

    def part(element, name):
        return next((c for c in element if local(c) == name), None)

    def emit(element):
        nonlocal supported
        if element is None:
            return ""
        tag = local(element)
        child = lambda name: emit(part(element, name))
        group = lambda name: "{" + child(name) + "}"
        if tag == "t":
            return element.text or ""
        if tag.endswith("Pr") or tag == "ctrlPr":
            return ""
        if tag == "f":
            return group("num") + " over " + group("den")
        if tag == "rad":
            degree = child("deg")
            return ("root {" + degree + "} of " if degree else "sqrt ") + group("e")
        if tag in {"sSup", "sSub", "sSubSup"}:
            return (group("e") + ("_" + group("sub") if tag != "sSup" else "")
                    + ("^" + group("sup") if tag != "sSub" else ""))
        if tag not in {"oMath", "oMathPara", "e", "num", "den", "deg", "sub", "sup", "r"}:
            supported = False
        return "".join(emit(c) for c in element)

    script = emit(node)
    return Equation(script, ET.tostring(node, encoding="utf-8"), supported=supported)


def to_omml(equation: Equation) -> tuple[bytes, bool]:
    if equation.omml:
        return equation.omml, equation.supported

    def element(name, *children, text=None):
        node = ET.Element(f"{{{M}}}{name}")
        node.extend(children)
        node.text = text
        return node

    def run(text):
        return element("r", element("t", text=text))

    tokens = re.findall(r"\s+|[{}_^]|[^\s{}_^]+", equation.script)
    tokens = [t for t in tokens if not t.isspace()]
    pos = 0

    def group():
        nonlocal pos
        if pos >= len(tokens):
            raise ValueError("missing equation operand")
        token = tokens[pos]
        pos += 1
        if token == "{":
            result = sequence("}")
            if pos >= len(tokens) or tokens[pos] != "}":
                raise ValueError("unbalanced equation")
            pos += 1
            return result
        if token == "sqrt":
            return [element("rad", element("radPr", element("degHide")), element("deg"), element("e", *group()))]
        if token == "root":
            degree = group()
            if pos >= len(tokens) or tokens[pos] != "of":
                raise ValueError("missing root of")
            pos += 1
            return [element("rad", element("deg", *degree), element("e", *group()))]
        if token in {"over", "}", "_", "^"}:
            raise ValueError("unexpected equation operator")
        if token.lower() in {"sum", "int", "matrix", "pmatrix", "cases", "left", "right", "pile"} or "#" in token or "&" in token:
            raise ValueError("unsupported equation structure")
        return [run(token)]

    def sequence(stop=None):
        nonlocal pos
        output = []
        while pos < len(tokens) and tokens[pos] != stop:
            current = group()
            sub = sup = None
            while pos < len(tokens) and tokens[pos] in {"_", "^"}:
                operator = tokens[pos]
                pos += 1
                value = group()
                if operator == "_":
                    sub = value
                else:
                    sup = value
            if sub is not None or sup is not None:
                name = "sSubSup" if sub is not None and sup is not None else "sSub" if sub is not None else "sSup"
                node = element(name, element("e", *current))
                if sub is not None:
                    node.append(element("sub", *sub))
                if sup is not None:
                    node.append(element("sup", *sup))
                current = [node]
            if pos < len(tokens) and tokens[pos] == "over":
                pos += 1
                current = [element("f", element("num", *current), element("den", *group()))]
            output.extend(current)
        return output

    try:
        root = element("oMath", *sequence())
        if pos != len(tokens):
            raise ValueError("trailing equation tokens")
        return ET.tostring(root, encoding="utf-8"), True
    except ValueError:
        return ET.tostring(element("oMath", run(equation.script)), encoding="utf-8"), False
