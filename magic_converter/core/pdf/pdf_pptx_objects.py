"""Reconstruct a slide from PDF paint order, never from the source PPTX."""

from __future__ import annotations

import io
import re
from collections import defaultdict

import pymupdf as fitz
from pptx.dml.color import RGBColor
from pptx.util import Pt

from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.pdf.pdf_layout import text_spans, extract_tables, assign_pictures, pictures, contains_center, rgb_hex
from magic_converter.core.pdf.pdf_paint import PdfPaint
from magic_converter.core.pdf.pdf_pptx_graphics import add_path, supported_path
from magic_converter.core.pdf.pdf_pptx_text import editable_font
from magic_converter.core.pdf.pdf_pptx_glyphs import glyph_outlines
from magic_converter.core.pdf.pdf_charts import extract_charts
from magic_converter.core.pdf.pdf_pptx_charts import add_chart


def _key(code, origin):
    return code, round(origin[0], 1), round(origin[1], 1)


def _text_operations(page, spans, paint, *, semantic=False):
    locations = defaultdict(set)
    expected = defaultdict(set)
    stroke = set()
    traces = page.get_texttrace()
    for trace in traces:
        sequence = trace["seqno"]
        for code, _, origin, _ in trace["chars"]:
            if code < 0 or chr(code).isspace():
                continue
            key = _key(code, origin)
            locations[key].add(sequence)
            expected[sequence].add(key)
        if trace["type"] != 0 or trace.get("opacity", 1) < .001:
            stroke.add(sequence)
    # Simulated bold is sometimes two almost identical regular-font paints.
    # Text extraction deduplicates them. Replacing only one would leave a
    # ghost behind when the textbox moves, so retain the overprint together.
    signatures = defaultdict(list)
    for trace in traces:
        if trace["chars"]:
            signature = (trace["font"], round(trace["size"], 2), tuple(trace["color"]),
                         tuple(c[0] for c in trace["chars"]))
            for previous in signatures[signature]:
                a, b = fitz.Point(trace["chars"][0][2]), fitz.Point(previous["chars"][0][2])
                if abs(a - b) <= min(1, trace["size"] * .08):
                    stroke.update((trace["seqno"], previous["seqno"]))
            signatures[signature].append(trace)
    candidates, coverage = {}, defaultdict(set)
    for index, span in enumerate(spans):
        if not semantic and editable_font(span) is None:
            continue
        sequences, keys, missing = set(), set(), False
        for character in span.characters:
            if character.text.isspace():
                continue
            key = _key(ord(character.text), character.origin)
            matched = locations.get(key)
            if not matched:
                missing = True
                break
            keys.add(key)
            sequences.update(matched)
        safe = (all(paint.native_safe({n}, fitz.Rect(paint.log[n][1]) + (1.5, 1.5, -1.5, -1.5)) for n in sequences)
                if semantic else paint.native_safe(sequences, span.bbox))
        # A chart label's font box can exceed its rectangular PDF clip even
        # though the visible glyphs fit. Small ink-padding differences are
        # acceptable when reconstructing recognised semantic chart labels.
        if (not sequences or missing or (not semantic and (sequences & stroke
                or any(not paint.states[n].native_text for n in sequences))) or not safe):
            continue
        # A span can combine several PDF text operators, but must not jump
        # over a graphic painted between them.
        if any(paint.log[n][0] not in {"fill-text", "ignore-text"} for n in range(min(sequences), max(sequences) + 1)):
            continue
        candidates[index] = sequences
        for sequence in sequences:
            coverage[sequence].update(keys & expected[sequence])
    # One PDF text operator may contain several lines. It can only be removed
    # from the raster replay when every visible character has a replacement.
    while candidates:
        incomplete = {sequence for sequences in candidates.values() for sequence in sequences
                      if coverage[sequence] != expected[sequence]}
        rejected = [index for index, sequences in candidates.items() if sequences & incomplete]
        if not rejected:
            break
        for index in rejected:
            for sequence in candidates.pop(index):
                coverage[sequence].difference_update(_key(ord(c.text), c.origin) for c in spans[index].characters if not c.text.isspace())
    return candidates


def _path_sequences(drawing, paint):
    sequence = drawing["seqno"]
    result = {sequence}
    if drawing["type"] == "fs":
        if sequence + 1 >= len(paint.log) or paint.log[sequence + 1][0] != "stroke-path":
            return set()
        result.add(sequence + 1)
    return result


def _table_grid_path(drawing, table):
    """A cell icon/diagram must not disappear when a grid becomes a table."""
    def near(value, coordinates):
        return any(abs(value - x) <= 1.2 for x in coordinates)
    for item in drawing["items"]:
        if item[0] == "re":
            r = fitz.Rect(item[1])
            if not (near(r.x0, table.xs) and near(r.x1, table.xs)
                    and near(r.y0, table.ys) and near(r.y1, table.ys)):
                return False
        elif item[0] == "l":
            a, b = item[1:]
            if not ((abs(a.x - b.x) < .1 and near(a.x, table.xs))
                    or (abs(a.y - b.y) < .1 and near(a.y, table.ys))):
                return False
        else:
            return False
    return True


def restore_page(page, slide, scale, offset_x, offset_y, add_text, add_table, warning, page_number):
    from magic_converter.core.pdf.pdf_paint import PaintSequenceMismatch
    try:
        paint = PdfPaint(page)
    except PaintSequenceMismatch:
        # Do not guess object ownership when MuPDF optimizes the display list
        # differently from bbox extraction. Preserve the whole visible page.
        dpi = min(200, 72 * (40_000_000 / (page.rect.width * page.rect.height)) ** .5)
        pixmap = page.get_pixmap(matrix=fitz.Matrix(dpi / 72, dpi / 72), alpha=False)
        shape = slide.shapes.add_picture(io.BytesIO(pixmap.tobytes('png')), Pt(offset_x), Pt(offset_y),
                                         Pt(page.rect.width * scale), Pt(page.rect.height * scale))
        shape.name = 'PDF preserved page (paint sequence mismatch)'
        warning(f'{page_number}쪽: PDF 그리기 순서를 개별 객체와 대조할 수 없어 쪽 전체를 그림으로 보존했습니다(편집 제한).')
        return dict(shapes=0, textboxes=0, tables=0, charts=0, pictures=0, preserved_regions=1)
    spans = text_spans(page)
    text_ops = _text_operations(page, spans, paint)
    drawings = page.get_drawings()
    emissions = defaultdict(list)
    consumed = {i for i, (kind, _) in enumerate(paint.log) if kind == "ignore-text" or paint.states[i].mask_paint}
    stats = {"shapes": 0, "textboxes": 0, "tables": 0, "charts": 0, "pictures": 0, "preserved_regions": 0}

    chart_spans = set()
    semantic_ops = None
    for chart in extract_charts(page, spans, drawings):
        if semantic_ops is None:
            semantic_ops = _text_operations(page, spans, paint, semantic=True)
        if any(index not in semantic_ops for index in chart.labels):
            continue
        text_sequences = set().union(*(semantic_ops[index] for index in chart.labels))
        # One PDF text operator may paint both a chart and an outside caption.
        # Removing it is safe only when every span is owned by this chart.
        if any(index not in chart.labels and sequences & text_sequences for index, sequences in semantic_ops.items()):
            continue
        sequences = text_sequences | chart.paths
        if sequences & consumed or any(n >= len(paint.log) for n in sequences):
            continue
        if not all(paint.native_safe({n}, paint.visible_bounds(n)) for n in sequences):
            continue
        emissions[max(sequences)].append(("chart", chart))
        consumed.update(sequences)
        chart_spans.update(chart.labels)
        for index in chart.labels:
            text_ops.pop(index, None)
        estimated = [(s.name, c) for s in chart.series for c, inferred in zip(chart.categories, s.estimated) if inferred]
        if estimated:
            warning(f"{page_number}쪽: 차트의 {len(estimated)}개 값은 PDF 도형·눈금으로 추정했습니다. "
                    "정확한 원본값은 확인할 수 없으며, 차트 데이터의 'PDF Recovery' 시트에 추정 여부를 표시했습니다.")

    tables = extract_tables(page, spans)
    accepted = []
    for table in tables:
        owners = [index for index, span in enumerate(spans) if contains_center(table.bbox, span.bbox)]
        if any(index not in text_ops for index in owners) or set(owners) & chart_spans:
            continue
        owned_sequences = set().union(*(text_ops[index] for index in owners)) if owners else set()
        if any(index not in owners and seqs & owned_sequences for index, seqs in text_ops.items()):
            continue
        sequences = set()
        ruled_paths = set()
        decorated = False
        for drawing in drawings:
            if (fitz.Rect(table.bbox) + (-1.1, -1.1, 1.1, 1.1)).contains(fitz.Rect(drawing["rect"])):
                if not _table_grid_path(drawing, table):
                    decorated = True
                    break
                ruled_paths.update(_path_sequences(drawing, paint))
        if decorated:
            continue
        for sequence, (kind, bounds) in enumerate(paint.log):
            if sequence in consumed:
                continue
            r = paint.visible_bounds(sequence)
            if r.is_empty or (sequence not in ruled_paths and not (fitz.Rect(table.bbox) + (-1.1, -1.1, 1.1, 1.1)).contains(r)):
                continue
            # Cell pictures are copied from their original rendered crops,
            # including soft masks/clipping; they need not be native-safe.
            if kind not in {"fill-path", "stroke-path", "fill-text", "fill-image"} or (kind != "fill-image" and not paint.native_safe({sequence}, r)):
                break
            sequences.add(sequence)
        else:
            if sequences and all(text_ops[index] <= sequences for index in owners):
                accepted.append(table)
                emissions[max(sequences)].append(("table", table))
                consumed.update(sequences)
                for index in owners:
                    text_ops.pop(index)
    if accepted:
        assign_pictures(accepted, pictures(page))

    retained = [span for i, span in enumerate(spans) if i not in text_ops and i not in chart_spans
                and not any(contains_center(table.bbox, span.bbox) for table in accepted)]
    if retained:
        names = ", ".join(sorted({span.font for span in retained}))
        warning(f"{page_number}쪽: 폰트·클리핑·문자 배치를 복원하기 어려운 텍스트는 원래 모양으로 보존했습니다(편집 제한): {names}")
    if not spans and page.get_image_info():
        warning(f"{page_number}쪽은 스캔/이미지 페이지입니다. 표·글자 편집에는 OCR 및 표 구조 인식이 필요합니다.")
    before_text = set(consumed)
    for index, sequences in text_ops.items():
        if sequences & before_text:
            continue
        emissions[min(sequences)].append(("text", spans[index]))
        consumed.update(sequences)

    for drawing in drawings:
        sequences = _path_sequences(drawing, paint)
        if not sequences or sequences & consumed or not supported_path(drawing):
            continue
        # DrawingML has no dash-phase property.
        dash = re.match(r"\[[^]]*\]\s*([-+\d.]+)", drawing.get("dashes") or "[] 0")
        if dash and abs(float(dash[1])) > .001:
            continue
        if not paint.native_safe(sequences, drawing["rect"]):
            continue
        emissions[min(sequences)].append(("path", drawing))
        consumed.update(sequences)

    # Transparency groups with opacity/blending must be replayed as a whole,
    # rather than assigning group opacity separately to overlapping children.
    groups = sorted(paint.groups, key=lambda g: (g.start, -g.end))
    atomic = {}
    last_end = -1
    for group in groups:
        if group.start < last_end:
            continue
        atomic[group.start] = group
        last_end = group.end

    glyph_traces, outlined_regions = None, 0

    def picture(sequences, bounds, name, preserved=False):
        nonlocal glyph_traces, outlined_regions
        check_cancelled()
        sequences = tuple(sequences)
        if preserved and all(paint.log[n][0] in {"fill-text", "stroke-text"} for n in sequences):
            if glyph_traces is None:
                glyph_traces = {trace["seqno"]: trace for trace in page.get_texttrace()}
            outlines = glyph_outlines(paint, sequences, bounds, glyph_traces)
            if outlines:
                group = slide.shapes.add_group_shape()
                group.name = f"PDF glyph outline region {sequences[0] + 1}"
                cached_ids = slide.shapes.turbo_add_enabled
                if cached_ids:
                    group.shapes.turbo_add_enabled = True
                try:
                    for drawing in outlines:
                        add_path(group, drawing, scale, offset_x, offset_y)
                finally:
                    if cached_ids:
                        group.shapes.turbo_add_enabled = False
                        # Child IDs share the slide's namespace. Refresh the
                        # parent cache before the next top-level shape.
                        slide.shapes.turbo_add_enabled = True
                stats["shapes"] += len(outlines)
                outlined_regions += 1
                return
        rendered = paint.render(sequences, bounds)
        if rendered is None:
            return
        data, r = rendered
        shape = slide.shapes.add_picture(io.BytesIO(data), Pt(offset_x + r.x0 * scale),
                                         Pt(offset_y + r.y0 * scale), Pt(r.width * scale), Pt(r.height * scale))
        shape.name = name
        stats["preserved_regions" if preserved else "pictures"] += 1

    pending, pending_bounds = [], fitz.Rect()

    def flush():
        nonlocal pending, pending_bounds
        if pending:
            picture(pending, pending_bounds, f"PDF preserved region {pending[0] + 1}", True)
        pending, pending_bounds = [], fitz.Rect()

    sequence = 0
    while sequence < len(paint.log):
        check_cancelled()
        if sequence in emissions:
            flush()
            for kind, value in emissions[sequence]:
                if kind == "text":
                    first = len(slide.shapes)
                    add_text(slide, value, scale, offset_x, offset_y)
                    for index in range(first, len(slide.shapes)):
                        slide.shapes[index].name = f"PDF text {sequence + 1} region {index - first + 1}"
                    stats["textboxes"] += len(slide.shapes) - first
                elif kind == "path":
                    r = fitz.Rect(value["rect"])
                    # The first opaque, full-page rectangle is a page
                    # background, not a giant selectable picture/shape.
                    if sequence == 0 and abs(offset_x) + abs(offset_y) < .001 and value["fill"] is not None and value.get("color") is None and value.get("fill_opacity", 1) == 1 and (r + (-.1, -.1, .1, .1)).contains(page.rect):
                        slide.background.fill.solid()
                        slide.background.fill.fore_color.rgb = RGBColor.from_string(rgb_hex(value["fill"]))
                    else:
                        add_path(slide, value, scale, offset_x, offset_y)
                        stats["shapes"] += 1
                elif kind == "chart":
                    add_chart(slide, value, scale, offset_x, offset_y, page_number)
                    stats["charts"] += 1
                else:
                    positioned = add_table(slide, value, scale, offset_x, offset_y)
                    if positioned and positioned[0]:
                        stats["textboxes"] += positioned[1]
                        warning(f"{page_number}쪽: 줄 간격을 셀 문단으로 표현하기 어려운 {positioned[0]}개 셀의 문자는 표 위의 독립 텍스트 상자로 보존했습니다(셀 내부 문자 편집 제한).")
                    stats["tables"] += 1
        if sequence in atomic:
            flush()
            group = atomic[sequence]
            selected = range(0 if group.backdrop else group.start, group.end)
            picture(selected, group.bounds, f"PDF preserved effect {sequence + 1}", True)
            sequence = group.end
            continue
        if sequence not in consumed:
            kind, _ = paint.log[sequence]
            bounds = paint.visible_bounds(sequence)
            if not bounds.is_empty:
                if kind == "fill-image":
                    flush()
                    picture({sequence}, bounds, f"PDF image {sequence + 1}")
                else:
                    if pending and not (pending_bounds + (-2, -2, 2, 2)).intersects(bounds):
                        flush()
                    pending.append(sequence)
                    pending_bounds |= bounds
        sequence += 1
    flush()
    if stats["preserved_regions"]:
        warning(f"{page_number}쪽: 복잡한 효과·경로 {stats['preserved_regions']}개 영역은 원래 모양의 개별 그림으로 보존했습니다.")
    if outlined_regions:
        warning(f"{page_number}쪽: 글자 모양 {outlined_regions}개 영역을 윤곽선 도형으로 보존했습니다(문자 편집 제한).")
    return stats
