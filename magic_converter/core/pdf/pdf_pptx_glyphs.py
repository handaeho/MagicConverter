"""Bounded glyph outlines for simple PDF text paints that Office cannot shape.

These are grouped curves, not replacement character text. Complex clips,
transparency groups, bitmap/complex Type3 glyphs and oversized outlines keep replay.
"""
from __future__ import annotations

import math

import pymupdf as fitz

from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.pdf.pdf_paint import PaintDevice, _native_text_matrix

m = fitz.mupdf
MAX_GLYPHS = 1024
MAX_COMMANDS = 30000
MAX_OVERLAP_CHECKS = 4096
# Separate substantial overlaps. For tiny kerning intersections, retain the
# original compound edges and fill only the cancelled intersection instead.
MIN_OPAQUE_OVERLAP_RATIO = .1
# Curve intersections and tiny polygon edges remain dominated by coverage.
MIN_REPAIR_INK_EXTENT_PT = 1
MIN_LINEAR_REPAIR_INK_EXTENT_PT = .25
CLIPPED_GLYPH_GAP_PAD_PT = .5


class _UnsupportedOutline(ValueError):
    pass


def _filled_ink_overlaps(pieces, bounds):
    """Return a split decision and bounded repairs for small parity holes."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainterPath
    from magic_converter.core.pdf.pdf_pptx_graphics import path_commands

    paths, ink_areas, checks, repairs = {}, {}, 0, []
    budget = [MAX_COMMANDS]
    # Qt preserves polygon intersections without approximating Bezier curves.
    # Below a quarter point, edge coverage still dominates the visible ink.
    minimum_extent = (MIN_LINEAR_REPAIR_INK_EXTENT_PT
                      if all(item[0] in {'l', 'close', 're', 'qu'} for piece in pieces for item in piece)
                      else MIN_REPAIR_INK_EXTENT_PT)

    def filled_area(filled):
        area, points_used = 0, 0
        for polygon in filled.toFillPolygons():
            points_used += len(polygon)
            if points_used > MAX_COMMANDS:
                raise _UnsupportedOutline('Glyph overlap polygon budget exceeded')
            if not polygon:
                continue
            origin = polygon[0]
            area += abs(sum((polygon[n].x() - origin.x()) * (polygon[(n + 1) % len(polygon)].y() - origin.y())
                            - (polygon[(n + 1) % len(polygon)].x() - origin.x()) * (polygon[n].y() - origin.y())
                            for n in range(len(polygon))) / 2)
            check_cancelled()
        return area

    def path(index):
        if index not in paths:
            result = QPainterPath()
            result.setFillRule(Qt.WindingFill)
            for command, points in path_commands({'items': pieces[index]}):
                if command == 'M':
                    result.moveTo(points[0].x, points[0].y)
                elif command == 'L':
                    result.lineTo(points[0].x, points[0].y)
                elif command == 'C':
                    result.cubicTo(*(v for p in points for v in (p.x, p.y)))
                else:
                    result.closeSubpath()
            paths[index] = result
        return paths[index]

    for index, box in enumerate(bounds):
        for previous in range(index):
            if not box.intersects(bounds[previous]):
                continue
            checks += 1
            if checks > MAX_OVERLAP_CHECKS:
                raise _UnsupportedOutline('Glyph overlap comparison budget exceeded')
            check_cancelled()
            # Keep PDF paint order stable. Qt's boolean curve flattening can
            # differ at subpixel edges when its operands are reversed.
            intersection = path(previous).intersected(path(index))
            if intersection.elementCount() > MAX_COMMANDS:
                raise _UnsupportedOutline('Glyph overlap geometry budget exceeded')
            if intersection.isEmpty() or intersection.boundingRect().isEmpty():
                continue
            for n in (index, previous):
                if n not in ink_areas:
                    ink_areas[n] = filled_area(path(n))
            # Relative to actual filled ink, not the large empty font box.
            minimum = max(ink_areas[index], ink_areas[previous]) * MIN_OPAQUE_OVERLAP_RATIO
            area = filled_area(intersection)
            if area > 0 and area >= minimum:
                return True, []
            if area > 0:
                box = intersection.boundingRect()
                if min(box.width(), box.height()) < minimum_extent:
                    continue
                # Splitting every glyph changes Office's edge rasterization.
                # The original compound already draws all exclusive ink;
                # only its even-coverage intersections need another fill.
                repairs.append(_intersection_items(intersection, budget))
    return False, repairs


def _intersection_items(path, budget):
    """Translate a Qt boolean result, retaining its contours and holes."""
    if path.elementCount() > budget[0]:
        raise _UnsupportedOutline('Glyph intersection repair budget exceeded')
    result, previous, index = [], None, 0
    while index < path.elementCount():
        budget[0] -= 1
        if budget[0] % 256 == 0:
            check_cancelled()
        element = path.elementAt(index)
        point = fitz.Point(element.x, element.y)
        if not all(math.isfinite(v) and abs(v) <= 10_000_000 for v in point):
            raise _UnsupportedOutline('Glyph intersection coordinates unsupported')
        if element.isMoveTo():
            if previous is not None:
                result.append(('close',))
        elif element.isLineTo():
            result.append(('l', previous, point))
        elif element.isCurveTo():
            controls = [path.elementAt(index + n) for n in (1, 2)]
            points = [fitz.Point(p.x, p.y) for p in controls]
            if any(not math.isfinite(v) or abs(v) > 10_000_000 for p in points for v in p):
                raise _UnsupportedOutline('Glyph intersection curve coordinates unsupported')
            result.append(('c', previous, point, *points))
            point = points[-1]
            index += 2
            budget[0] -= 2
        else:
            raise _UnsupportedOutline('Unknown glyph intersection element')
        previous = point
        index += 1
    if previous is not None:
        result.append(('close',))
    return result


def _stroke_fill(items, width, cap, join, miter, budget):
    """Keep the stroked silhouette independent of Office line joins/scaling."""
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainterPath, QPainterPathStroker
    from magic_converter.core.pdf.pdf_pptx_graphics import path_commands

    check_cancelled()
    path = QPainterPath()
    for command, points in path_commands({'items': items}):
        if command == 'M':
            path.moveTo(points[0].x, points[0].y)
        elif command == 'L':
            path.lineTo(points[0].x, points[0].y)
        elif command == 'C':
            path.cubicTo(*(value for point in points for value in (point.x, point.y)))
        else:
            path.closeSubpath()
    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    stroker.setCurveThreshold(.01)
    stroker.setCapStyle({0: Qt.FlatCap, 1: Qt.RoundCap, 2: Qt.SquareCap}[cap])
    # PDF bevels an over-limit miter; Qt.MiterJoin truncates it instead.
    stroker.setJoinStyle({0: Qt.SvgMiterJoin, 1: Qt.RoundJoin, 2: Qt.BevelJoin}[join])
    # PDF measures a miter against half the stroke width; Qt uses the width.
    stroker.setMiterLimit(miter / 2)
    outline = stroker.createStroke(path)
    check_cancelled()
    if outline.elementCount() > budget[0]:
        raise _UnsupportedOutline('Stroked glyph command budget exceeded')
    # Office applies parity filling to intersecting stroke contours. Merge
    # the covered areas first, so joins/crossings cannot turn into tiny holes.
    # Qt may flatten curves here; retain the same budget and clip fallbacks.
    outline = outline.simplified()
    check_cancelled()
    if outline.elementCount() > budget[0]:
        raise _UnsupportedOutline('Merged stroke command budget exceeded')
    result, previous, index = [], None, 0
    while index < outline.elementCount():
        budget[0] -= 1
        if budget[0] % 256 == 0:
            check_cancelled()
        element = outline.elementAt(index)
        point = fitz.Point(element.x, element.y)
        if not all(math.isfinite(v) and abs(v) <= 10_000_000 for v in point):
            raise _UnsupportedOutline('Stroked glyph coordinates unsupported')
        if element.isMoveTo():
            if previous is not None:
                result.append(('close',))
        elif element.isLineTo():
            result.append(('l', previous, point))
        elif element.isCurveTo():
            controls = [outline.elementAt(index + n) for n in (1, 2)]
            points = [fitz.Point(p.x, p.y) for p in controls]
            if any(not math.isfinite(v) or abs(v) > 10_000_000 for p in points for v in p):
                raise _UnsupportedOutline('Stroked glyph curve coordinates unsupported')
            result.append(('c', previous, point, *points))
            point = points[-1]
            index += 2
            budget[0] -= 2
        else:
            raise _UnsupportedOutline('Unknown stroked glyph path element')
        previous = point
        index += 1
    if previous is not None:
        result.append(('close',))
    return result


class _Walker(m.FzPathWalker2):
    def __init__(self, budget):
        super().__init__()
        self.items, self.current = [], None
        self.budget = budget
        self.moves = 0
        for name in ("moveto", "lineto", "curveto", "closepath"):
            getattr(self, "use_virtual_" + name)()

    def _take(self, *points):
        self.budget[0] -= 1
        if self.budget[0] < 0:
            return False
        if self.budget[0] % 256 == 0:
            check_cancelled()
        if any(not math.isfinite(v) or abs(v) > 10_000_000 for p in points for v in p):
            self.budget[0] = -1
            return False
        return True

    def moveto(self, argument, x, y):
        self.moves += 1
        self.current = fitz.Point(x, y)
        self._take(self.current)

    def lineto(self, argument, x, y):
        point = fitz.Point(x, y)
        if self._take(point):
            self.items.append(("l", self.current, point))
        self.current = point

    def curveto(self, argument, *coordinates):
        points = [fitz.Point(*coordinates[i:i + 2]) for i in range(0, 6, 2)]
        if self._take(*points):
            self.items.append(("c", self.current, *points))
        self.current = points[-1]

    def closepath(self, argument):
        if self._take():
            self.items.append(("close",))
        self.current = None


class _Type3PathDevice(PaintDevice):
    """Capture exactly one opaque black nonzero path; reject every other paint."""
    def __init__(self, budget, single_contour):
        super().__init__(fitz.Rect(0, 0, 1, 1))
        self.budget, self.items, self.operations, self.unsupported = budget, [], 0, False
        self.single_contour = single_contour

    def _call(self, name, args):
        self.operations += 1
        check_cancelled()
        if self.unsupported or name != 'fill_path' or self.operations != 1:
            self.unsupported = True
            return 0 if name == 'begin_tile' else None
        colorspace = args[3]
        if (args[1] or args[5] != 1 or not colorspace
                or not m.ll_fz_colorspace_is_device(colorspace)
                or m.ll_fz_colorspace_n(colorspace) not in {1, 3}):
            self.unsupported = True
            return
        if any(m.floats_getitem(args[4], n) != 0 for n in range(m.ll_fz_colorspace_n(colorspace))):
            self.unsupported = True
            return
        walker = _Walker(self.budget)
        m.fz_walk_path(m.FzPath(m.ll_fz_keep_path(args[0])), walker, walker.m_internal)
        if self.single_contour and walker.moves != 1:
            self.unsupported = True
            return
        ctm = args[2]
        matrix = fitz.Matrix(ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)
        for item in walker.items:
            points = [p * matrix for p in item[1:]]
            if any(not math.isfinite(v) or abs(v) > 10_000_000 for p in points for v in p):
                self.unsupported = True
                return
            self.items.append((item[0], *points))


def _type3_items(font, gid, matrix, budget, *, single_contour=False):
    device = _Type3PathDevice(budget, single_contour)
    m.fz_run_t3_glyph(font, gid, fitz.JM_matrix_from_py(matrix), device)
    if device.unsupported or device.operations != 1 or not device.items or budget[0] < 0:
        raise _UnsupportedOutline('Type3 glyph needs original replay')
    if all(item[0] in {'l', 'close'} for item in device.items):
        from magic_converter.core.pdf.pdf_type3 import linear_nonzero_items
        try:
            return linear_nonzero_items(device.items, MAX_OVERLAP_CHECKS)
        except ValueError as error:
            raise _UnsupportedOutline(str(error)) from error
    from magic_converter.core.pdf.pdf_type3_curves import convex_nonzero_items
    try:
        return convex_nonzero_items(device.items, MAX_OVERLAP_CHECKS)
    except ValueError as error:
        # Extend only the initial convexity refusal. Never restart a failed
        # refinement or an exhausted budget with another full allowance.
        if str(error) != 'Nonconvex Type3 control boundary':
            raise _UnsupportedOutline(str(error)) from error
        from magic_converter.core.pdf.pdf_type3_simple_curves import simple_curve_items
        try:
            initial_cost = sum(len(item)-1 for item in device.items) ** 2
            return simple_curve_items(device.items, MAX_OVERLAP_CHECKS-initial_cost)
        except ValueError:
            raise _UnsupportedOutline(str(error)) from error


def _clip_convex_glyph(items, clip, budget, checks):
    """Intersect one strictly convex linear contour with a rectangular clip."""
    if not (3 <= len(items) <= 65 and items[-1][0] == 'close'
            and all(item[0] == 'l' for item in items[:-1])):
        raise _UnsupportedOutline('Partial clip needs a bounded linear contour')
    edges = items[:-1]
    if any(edge[2] != following[1] for edge, following in zip(edges[:-1], edges[1:])):
        raise _UnsupportedOutline('Discontinuous clipped glyph contour')
    vertices = [edge[1] for edge in edges]
    if edges[-1][2] != vertices[0]:
        vertices.append(edges[-1][2])
    if not 3 <= len(vertices) <= 64:
        raise _UnsupportedOutline('Clipped glyph vertex budget exceeded')
    checks[0] -= len(vertices) ** 2
    if checks[0] < 0:
        raise _UnsupportedOutline('Clipped glyph comparison budget exceeded')

    # Every other vertex must lie strictly on the interior side of each
    # directed edge. This also rejects self-intersection and repeated edges.
    direction = 0
    for n, a in enumerate(vertices):
        check_cancelled()
        following = (n + 1) % len(vertices)
        b = vertices[following]
        for k, c in enumerate(vertices):
            if k in (n, following):
                continue
            cross = (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
            if not direction:
                direction = 1 if cross > 0 else -1
            if direction * cross <= 1e-6:
                raise _UnsupportedOutline('Partial clip needs a strictly convex glyph')

    for axis, boundary, inward in ((0, clip.x0, 1), (0, clip.x1, -1),
                                   (1, clip.y0, 1), (1, clip.y1, -1)):
        result = []
        for a, b in zip(vertices[-1:] + vertices[:-1], vertices):
            a_inside = inward * (a[axis] - boundary) >= 0
            b_inside = inward * (b[axis] - boundary) >= 0
            if a_inside != b_inside:
                other = 1 - axis
                ratio = (boundary - a[axis]) / (b[axis] - a[axis])
                coordinate = a[other] + ratio * (b[other] - a[other])
                point = fitz.Point(boundary, coordinate) if axis == 0 else fitz.Point(coordinate, boundary)
                if not result or point != result[-1]:
                    result.append(point)
            if b_inside and (not result or b != result[-1]):
                result.append(b)
        if result and result[0] == result[-1]:
            result.pop()
        if len(result) < 3:
            raise _UnsupportedOutline('Clipped glyph has no provable ink')
        vertices = result
    budget[0] -= len(vertices) + 1
    if budget[0] < 0:
        raise _UnsupportedOutline('Clipped glyph command budget exceeded')
    return [('l', a, b) for a, b in zip(vertices, vertices[1:] + vertices[:1])] + [('close',)]


class _OutlineDevice(PaintDevice):
    def __init__(self, paint, selected, traces, clipped):
        super().__init__(paint.rect)
        self.selected, self.traces = set(selected), traces
        self.paint_states = paint.states
        self.clipped = clipped
        self.drawings, self.seen = [], set()
        self.budget, self.glyphs = [MAX_COMMANDS], 0
        self.clip_checks = [MAX_OVERLAP_CHECKS]
        self.unsupported = False

    def _outline(self, name, args):
        trace = self.traces[self.count]
        partial_clip = self.count in self.clipped
        mask_alpha = self.paint_states[self.count].mask_alpha
        opacity = trace['opacity'] * (mask_alpha if mask_alpha is not None else 1)
        if not math.isfinite(opacity) or not 0 < opacity <= 1 or trace["colorspace"] not in {1, 3}:
            raise _UnsupportedOutline("Text needs colour/transparency replay")
        if partial_clip and opacity != 1:
            raise _UnsupportedOutline('Partial clip needs opaque glyphs')
        stroke = name == "stroke_text"
        if mask_alpha is not None and stroke:
            raise _UnsupportedOutline('Masked stroke needs original replay')
        first_glyph = self.glyphs
        ctm = args[2 if stroke else 1]
        width, cap, join, miter = 0, 0, 0, 1
        if stroke:
            state = args[1]
            sx, sy = math.hypot(ctm.a, ctm.b), math.hypot(ctm.c, ctm.d)
            if (not math.isfinite(sx + sy) or min(sx, sy) <= 0
                    or abs(sx - sy) > max(sx, sy) * .001
                    or abs(ctm.a * ctm.c + ctm.b * ctm.d) > sx * sy * .001
                    or state.dash_len or state.start_cap != state.end_cap
                    or state.linejoin not in {0, 1, 2} or state.start_cap not in {0, 1, 2}):
                raise _UnsupportedOutline("Stroke needs original replay")
            width, cap, join, miter = state.linewidth * sx, state.start_cap, state.linejoin, state.miterlimit
            if (not all(math.isfinite(v) and v > 0 for v in (width, miter))
                    or width < .05 or miter < 1 or miter > 20000):
                raise _UnsupportedOutline("Hairline or invalid stroke")
        span, items, ink_bounds = args[0].head, [], []
        pieces, ink_pieces, overlaps = [], [], False
        empty_glyphs = 0
        while span:
            wrapped = m.FzTextSpan(span)
            for index in range(span.len):
                glyph = wrapped.items(index)
                if glyph.gid < 0:
                    continue
                self.glyphs += 1
                if self.glyphs > MAX_GLYPHS:
                    raise _UnsupportedOutline("Glyph count budget exceeded")
                if self.glyphs % 64 == 0:
                    check_cancelled()
                matrix = fitz.Matrix(span.trm.a, span.trm.b, span.trm.c, span.trm.d, glyph.x, glyph.y)
                matrix *= fitz.Matrix(ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)
                font = wrapped.font()
                path = m.fz_outline_glyph(font, glyph.gid, fitz.JM_matrix_from_py(matrix))
                if path.m_internal:
                    walker = _Walker(self.budget)
                    m.fz_walk_path(path, walker, walker.m_internal)
                    glyph_items = walker.items
                    if partial_clip and glyph_items and walker.moves != 1:
                        raise _UnsupportedOutline('Partial clip needs a single contour')
                elif (not stroke and trace['opacity'] == 1 and all(v == 0 for v in trace['color'])
                      and font.fz_font_t3_procs() and 0 <= glyph.gid <= 255):
                    glyph_items = _type3_items(font, glyph.gid, matrix, self.budget, single_contour=partial_clip)
                else:
                    raise _UnsupportedOutline("Font does not supply a supported vector outline")
                if self.budget[0] < 0:
                    raise _UnsupportedOutline("Glyph path budget or coordinates unsupported")
                # The actual supplied outline can contain no drawing commands
                # (e.g. an ordinary space). It adds no ink to mask compositing.
                # Visible paths mapped to whitespace are deliberately retained.
                if not glyph_items:
                    empty_glyphs += 1
                    continue
                if partial_clip:
                    clip = self.paint_states[self.count].clip
                    if any(item[0] == 'c' for item in glyph_items):
                        from magic_converter.core.pdf.pdf_cubic_clip import clip_cubic_glyph
                        glyph_items = clip_cubic_glyph(glyph_items, clip, self.budget, self.clip_checks)
                    else:
                        glyph_items = _clip_convex_glyph(glyph_items, clip, self.budget, self.clip_checks)
                if glyph_items and (not stroke or opacity < 1):
                    points = [p for item in glyph_items for p in item[1:]]
                    if points:
                        ink = fitz.Rect(min(p.x for p in points), min(p.y for p in points),
                                        max(p.x for p in points), max(p.y for p in points))
                        # Office also cancels intersections in an opaque
                        # compound glyph path. Preserve each overlapping glyph
                        # as its own shape, including opaque ink and counters;
                        # translucent ink additionally needs separate alpha.
                        pad = width * max(1, miter) / 2 if stroke else 0
                        # A constant mask may be distributed over several
                        # glyphs only when their entire outline boxes have a
                        # strict gap. Touching/overlapping masked ink keeps
                        # the original compositing, including antialiasing.
                        if mask_alpha is not None:
                            pad = max(pad, .01)
                        if partial_clip:
                            # A compound Office fill is equivalent only for
                            # separated clipped ink, including edge coverage.
                            pad = CLIPPED_GLYPH_GAP_PAD_PT
                            self.clip_checks[0] -= len(ink_bounds)
                            if self.clip_checks[0] < 0:
                                raise _UnsupportedOutline('Clipped glyph comparison budget exceeded')
                        ink += (-pad, -pad, pad, pad)
                        if any(ink.intersects(other) for other in ink_bounds):
                            if partial_clip:
                                raise _UnsupportedOutline('Clipped glyph ink must be disjoint')
                            overlaps = True
                        ink_bounds.append(ink)
                        ink_pieces.append(glyph_items)
                pieces.append(glyph_items)
                items.extend(glyph_items)
            span = span.next
        masked_glyphs = self.glyphs - first_glyph - empty_glyphs
        if (mask_alpha is not None and masked_glyphs != 1
                and (overlaps or len(ink_bounds) != masked_glyphs)):
            raise _UnsupportedOutline('Unproven masked multi-glyph compositing needs original replay')
        # A simple, non-overlapping stroke already maps to an Office line.
        # Expanding it can change the rasterizer's serif/curve antialiasing.
        # Only replace that representation when geometry or accumulated
        # translucent ink actually requires the filled silhouette.
        repairs = []
        if overlaps and not stroke and opacity == 1:
            overlaps, repairs = _filled_ink_overlaps(ink_pieces, ink_bounds)
            self.budget[0] -= sum(len(repair) for repair in repairs)
            if self.budget[0] < 0:
                raise _UnsupportedOutline('Glyph intersection total budget exceeded')
        expanded_stroke = stroke and (overlaps or not _native_text_matrix(args[0], ctm))
        shared_bounds = None
        if (overlaps or repairs) and not stroke and opacity == 1:
            # Keep the original compound path's coordinate scale. Giving
            # every glyph a newly rounded Office transform shifts its edges
            # slightly, even where the glyphs do not intersect.
            points = [p for item in items for p in item[1:]]
            shared_bounds = fitz.Rect(min(p.x for p in points), min(p.y for p in points),
                                      max(p.x for p in points), max(p.y for p in points))
        for items in (pieces if overlaps else [items]) + repairs:
            if not items:
                continue
            if expanded_stroke:
                items = _stroke_fill(items, width, cap, join, miter, self.budget)
            points = [p for item in items for p in item[1:]]
            if not points:
                raise _UnsupportedOutline("Empty glyph path")
            bounds = shared_bounds or fitz.Rect(min(p.x for p in points), min(p.y for p in points),
                                                max(p.x for p in points), max(p.y for p in points))
            if bounds.is_empty:
                raise _UnsupportedOutline("Degenerate glyph path")
            self.drawings.append({"seqno": self.count, "rect": bounds, "items": items,
                                  "fill": trace["color"] if not stroke or expanded_stroke else None,
                                  "color": trace["color"] if stroke and not expanded_stroke else None,
                                  "fill_opacity": opacity, "stroke_opacity": opacity,
                                  "width": width, "lineCap": (cap,), "lineJoin": join,
                                  "miter_limit": miter, "dashes": "[] 0"})
        self.seen.add(self.count)

    def _call(self, name, args):
        if not self.unsupported and name in {"fill_text", "stroke_text"} and self.count in self.selected:
            try:
                self._outline(name, args)
            except (ValueError, RuntimeError, OverflowError):
                # Reject within Python, without throwing ordinary fallback
                # decisions through the native device's director callbacks.
                check_cancelled()
                self.unsupported = True
        return super()._call(name, args)


def glyph_outlines(paint, sequences, bounds, traces):
    """Return every selected paint as curves, or None without emitting shapes."""
    selected = set(sequences)
    if not selected or any(n not in traces or paint.log[n][0] not in {"fill-text", "stroke-text"} for n in selected):
        return None
    if not paint.native_safe(selected, bounds, allow_uniform_mask=True):
        return None
    # Visible bounds have already been clipped. Check the original ink too;
    # otherwise a clipped glyph would grow back outside its rectangular clip.
    clipped = set()
    for n in selected:
        state = paint.states[n]
        if (state.clip + (-.3, -.3, .3, .3)).contains(fitz.Rect(paint.log[n][1])):
            continue
        if not state.exact_clip or state.clip == paint.rect or paint.log[n][0] != 'fill-text':
            return None
        clipped.add(n)
    try:
        device = _OutlineDevice(paint, selected, traces, clipped)
        paint._run(device, fitz.Matrix(1, 1))
        if device.unsupported or device.count != len(paint.log) or device.seen != selected:
            return None
        for drawing in device.drawings:
            sequence = drawing['seqno']
            clip = paint.states[sequence].clip
            if sequence not in clipped:
                clip = clip + (-.3, -.3, .3, .3)
            if not clip.contains(drawing['rect']):
                return None
        return device.drawings or None
    except (ValueError, RuntimeError, OverflowError):
        # JobCancelled/JobTimedOut are deliberately not swallowed here.
        check_cancelled()
        return None
