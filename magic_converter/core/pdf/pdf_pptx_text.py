"""Native, fixed-line PowerPoint text from PDF baselines and character advances."""

from __future__ import annotations

import math
import unicodedata
from dataclasses import replace

import pymupdf as fitz
from pptx.dml.color import RGBColor
from pptx.oxml.xmlchemy import OxmlElement
from pptx.oxml.ns import qn
from pptx.util import Pt

from magic_converter.core.pdf.pdf_fonts import resolve_font, font_metrics
from magic_converter.core.pdf.pdf_layout import PdfTextSpan


def positioned_spans(span: PdfTextSpan) -> tuple[PdfTextSpan, ...]:
    """Bound Office's cumulative CJK advance rounding with source anchors.

    PowerPoint quantizes small character spacing on export, even with wrap
    disabled. Short, separately editable regions preserve the original line
    instead of accumulating that error across a long CJK span. Do not split
    Latin words or Unicode clusters whose shaping needs their neighbours.
    """
    characters = span.characters
    def cjk(text):
        return any('\u2e80' <= c <= '\u9fff' or '\uac00' <= c <= '\ud7af'
                   or '\U00020000' <= c <= '\U0003134f' for c in text)
    if (len(characters) < 24 or sum(cjk(c.text) for c in characters) < len(characters) * .5
            or any(unicodedata.combining(c) or c in '\u200c\u200d'
                   or '\ufe00' <= c <= '\ufe0f' or '\U000e0100' <= c <= '\U000e01ef'
                   for c in span.text)):
        return (span,)
    starts = [0]
    for index in range(8, len(characters)):
        a, b = characters[index - 1].text, characters[index].text
        if index - starts[-1] >= 8 and (a.isspace() or b.isspace() or cjk(a) and cjk(b)):
            starts.append(index)
    if len(starts) == 1:
        return (span,)
    direction = fitz.Point(span.direction)
    ul, ur, ll, _ = (fitz.Point(p) for p in span.quad)
    result = []
    for start, end in zip(starts, starts[1:] + [len(characters)]):
        selected = characters[start:end]
        delta = fitz.Point(selected[0].origin) - fitz.Point(span.origin)
        width = ((fitz.Point(characters[end].origin) - fitz.Point(selected[0].origin)) * direction
                 if end < len(characters) else abs(ur - ul) - delta * direction)
        # Keep ambiguous/non-monotonic geometry in its original span.
        if width <= 0 or not math.isfinite(width):
            return (span,)
        left, lower = ul + delta, ll + delta
        quad = tuple(tuple(p) for p in (left, left + direction * width,
                                       lower, lower + direction * width))
        result.append(replace(span, text=''.join(c.text for c in selected), characters=selected,
                              origin=selected[0].origin, quad=quad, bbox=tuple(fitz.Quad(quad).rect)))
    return tuple(result)


def editable_font(span: PdfTextSpan):
    if span.writing_mode:
        return None
    face = resolve_font(span.font, bool(span.flags & 16), bool(span.flags & 2))
    if face is None:
        return None
    try:
        font = font_metrics(face)
        # Do not turn missing symbol glyphs into tofu or reshape a right-to-left
        # / vertical / skewed span using left-to-right character advances.
        ul, ur, ll, _ = (fitz.Point(p) for p in span.quad)
        horizontal, vertical = ur - ul, ll - ul
        if abs(horizontal.x * vertical.x + horizontal.y * vertical.y) > max(1, abs(horizontal) * abs(vertical)) * .02:
            return None
        if any(not font.has_glyph(ord(c)) for c in span.text if not c.isspace()):
            return None
        for first, last in zip(span.characters, span.characters[1:]):
            delta = (last.origin[0] - first.origin[0]) * span.direction[0] + (last.origin[1] - first.origin[1]) * span.direction[1]
            if delta < -.1:
                return None
    except (ValueError, RuntimeError, OSError):
        return None
    return face


def set_east_asian_font(run, name: str) -> None:
    properties = run._r.get_or_add_rPr()
    font = properties.find(qn("a:ea"))
    if font is None:
        font = OxmlElement("a:ea")
        latin = properties.find(qn("a:latin"))
        properties.insert(list(properties).index(latin) + 1 if latin is not None else len(properties), font)
    font.set("typeface", name)


def _run(paragraph, text: str, spacing: int, span: PdfTextSpan, scale: float, family: str):
    run = paragraph.add_run()
    run.text = text
    run.font.name = family
    set_east_asian_font(run, family)
    run.font.size = Pt(span.size * scale)
    run.font.bold, run.font.italic = bool(span.flags & 16), bool(span.flags & 2)
    run.font.color.rgb = RGBColor.from_string(f"{span.color:06X}")
    if span.alpha < 255:
        color = run._r.get_or_add_rPr().find(qn("a:solidFill")).find(qn("a:srgbClr"))
        alpha = OxmlElement("a:alpha")
        alpha.set("val", str(round(span.alpha / 255 * 100000)))
        color.append(alpha)
    properties = run._r.get_or_add_rPr()
    properties.set("lang", "ko-KR" if any(ord(c) >= 0x2E80 for c in text) else "en-US")
    properties.set("spc", str(max(-400000, min(400000, spacing))))
    # PDF advances already include kerning. Letting Office kern a second time
    # shifts later characters, even when the correct font has been selected.
    properties.set("kern", "400000")
    return run


def add_positioned_runs(paragraph, span: PdfTextSpan, scale: float) -> None:
    face = resolve_font(span.font, bool(span.flags & 16), bool(span.flags & 2))
    if face is None:
        return
    font = font_metrics(face)
    chunks: list[tuple[str, int]] = []

    def append(text, spacing):
        if chunks and chunks[-1][1] == spacing:
            chunks[-1] = chunks[-1][0] + text, spacing
        else:
            chunks.append((text, spacing))

    characters = span.characters
    if not characters:
        _run(paragraph, span.text, 0, span, scale, face.family)
        return
    space = font.glyph_advance(32) * span.size
    for index, char in enumerate(characters):
        spacing = 0.0
        insert_space = False
        if index + 1 < len(characters):
            following = characters[index + 1]
            advance = ((following.origin[0] - char.origin[0]) * span.direction[0]
                       + (following.origin[1] - char.origin[1]) * span.direction[1])
            spacing = advance - font.text_length(char.text, fontsize=span.size)
            # PDF exporters often encode word spaces as positioning operators,
            # without an actual U+0020. Restore them for subsequent editing.
            insert_space = (not char.text.isspace() and not following.text.isspace()
                            and space > 0 and spacing > max(space * .6, span.size * .12))
            if insert_space:
                spacing -= space
        append(char.text, round(spacing * scale * 100))
        if insert_space:
            append(" ", 0)
    for text, spacing in chunks:
        _run(paragraph, text, spacing, span, scale, face.family)


def configure_paragraph(paragraph) -> None:
    paragraph.space_before = paragraph.space_after = Pt(0)
    properties = paragraph._p.get_or_add_pPr()
    properties.set("fontAlgn", "base")
    properties.set("latinLnBrk", "0")
    properties.set("hangingPunct", "0")


def cell_vertical_layout(cell, cell_lines):
    """Preserve stored baselines when exact paragraph leading fits the cell.

    DrawingML puts a baseline at 3/4 of sufficiently large exact leading.
    The distance between separate paragraphs includes the remaining 1/4 of
    the preceding line and 3/4 of the next. Do not replace dense, reversed or
    ambiguous text geometry with negative paragraph spacing.
    """
    if not cell_lines or any(not line for line in cell_lines):
        return None
    baselines = [max(line, key=lambda span: span.size).origin[1] for line in cell_lines]
    if any(span.writing_mode or not all(math.isfinite(v) for v in span.direction)
           or abs(span.direction[0] - 1) > .001 or abs(span.direction[1]) > .001
           for line in cell_lines for span in line):
        return None
    for compact in (False, True):
        if compact:
            if any(not all(math.isfinite(v) for v in (s.ascender, s.descender))
                   for line in cell_lines for s in line):
                return None
            # The font box must fit above/below the 3/4 baseline. Smaller
            # leading than this invokes Office's font-dependent rounding.
            leading = [max(max(s.size * 1.5, s.size * s.ascender / .75,
                               -s.size * s.descender / .25) for s in line) for line in cell_lines]
        else:
            leading = [max(span.size for span in line) * 2 for line in cell_lines]
        top = baselines[0] - cell.bbox[1] - leading[0] * .75
        after = [b - a - first * .25 - second * .75
                 for a, b, first, second in zip(baselines, baselines[1:], leading, leading[1:])]
        values = [top, *baselines, *leading, *after]
        if (all(math.isfinite(value) for value in values) and top >= 0
                and all(value >= -.01 for value in after)
                and baselines[-1] + leading[-1] * .25 <= cell.bbox[3]):
            return top, leading, [max(0, value) for value in after] + [0]
    return None


def span_geometry(span: PdfTextSpan):
    ul, ur, ll, lr = (fitz.Point(point) for point in span.quad)
    # Recovered font quads can be slightly skewed even for horizontal text
    # (e.g. after non-uniform PDF scaling). Their upper edge is not a baseline.
    # Such artificial rotations also make PowerPoint export glyphs as paths.
    rotation = math.degrees(math.atan2(span.direction[1], span.direction[0])) % 360
    width, height = abs(ur - ul), abs(ll - ul)
    center = (ul + lr) / 2
    origin = fitz.Point(span.origin) - ul
    direction = fitz.Point(span.direction)
    baseline = origin.x * -direction.y + origin.y * direction.x
    leading = max(span.size * 2, height * 1.5)
    # An exact line taller than the font has a baseline at 3/4 of its leading
    # in DrawingML. A signed inset anchors it to the PDF baseline while keeping
    # the shape's selection bounds at the original ink/line rectangle.
    inset_top = baseline - leading * .75
    return center.x - width / 2, center.y - height / 2, width, height, rotation, leading, inset_top
