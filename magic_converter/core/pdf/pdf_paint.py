"""Isolate original PDF paint operations without discarding clips or masks.

This uses the same MuPDF device interface as PyMuPDF's bbox/text/drawing
extractors. All graphics state is replayed; only selected paint calls reach
the draw device. No PDF operators or source streams are rewritten.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import pymupdf as fitz

from magic_converter.core.jobs.job_control import check_cancelled

m = fitz.mupdf
# Retrying original interpretation can expose thousands of otherwise culled
# mask/shading operations. Keep exceptional recovery bounded; dense pages keep
# the existing whole-page preservation path instead of an unbounded retry.
MAX_PAGE_RETRY_PAINTS = 4096


class PaintSequenceMismatch(ValueError):
    """Extraction and replay disagree, so individual objects cannot be removed."""

PAINT = {"fill_path", "stroke_path", "fill_text", "stroke_text", "ignore_text",
         "fill_shade", "fill_image", "fill_image_mask"}
STATE = {"clip_path", "clip_stroke_path", "clip_text", "clip_stroke_text", "clip_image_mask",
         "pop_clip", "begin_mask", "end_mask", "begin_group", "end_group", "begin_tile", "end_tile",
         "render_flags", "set_default_colorspaces", "begin_layer", "end_layer"}


@dataclass
class PaintState:
    clip: fitz.Rect
    simple: bool
    mask_paint: bool
    native_text: bool = True
    mask_alpha: float | None = None
    exact_clip: bool = False


def _native_text_matrix(text, ctm):
    """DrawingML text cannot reproduce stretched, skewed or mirrored glyphs.

    MuPDF's extracted size is the geometric mean of the two axis scales.
    Character spacing restores origins, but cannot restore the glyph outlines.
    Inspect every span of the actual paint rather than guessing from its bbox.
    """
    span = text.head
    while span:
        matrix = fitz.Matrix(span.trm.a, span.trm.b, span.trm.c, span.trm.d, 0, 0)
        matrix *= fitz.Matrix(ctm.a, ctm.b, ctm.c, ctm.d, 0, 0)
        x = math.hypot(matrix.a, matrix.b)
        y = math.hypot(matrix.c, matrix.d)
        if (not all(math.isfinite(v) for v in (x, y)) or min(x, y) <= 0
                or abs(x - y) > max(x, y) * .01
                or abs(matrix.a * matrix.c + matrix.b * matrix.d) > x * y * .01
                or matrix.a * matrix.d - matrix.b * matrix.c >= 0):
            return False
        span = span.next
    return True


@dataclass
class PaintGroup:
    start: int
    end: int
    bounds: fitz.Rect
    backdrop: bool
    backdrop_start: int = 0


class _RectangleWalker(m.FzPathWalker2):
    def __init__(self):
        super().__init__()
        self.points, self.valid = [], True
        self.closed = self.closed_early = False
        for name in ("moveto", "lineto", "curveto", "closepath"):
            getattr(self, "use_virtual_" + name)()

    def moveto(self, argument, x, y):
        if self.points:
            self.valid = False
        self.points.append((x, y))

    def lineto(self, argument, x, y):
        if self.closed:
            self.closed_early = True
        if len(self.points) < 6:
            self.points.append((x, y))
        else:
            self.valid = False

    def curveto(self, argument, *coordinates):
        self.valid = False

    def closepath(self, argument):
        self.closed = True


def _rectangular_clip(path, ctm, *, exact=False):
    if not exact and m.ll_fz_path_is_rect(path, ctm):
        return True
    # Inspect the actual path, rather than its bounds: a circular clip can
    # have exactly the same bounding rectangle as a neighbouring rectangle.
    walker = _RectangleWalker()
    # SWIG's path-walker director identifies its Python instance through the
    # userdata argument (unlike FzDevice2 callbacks).
    m.fz_walk_path(m.FzPath(m.ll_fz_keep_path(path)), walker, walker.m_internal)
    matrix = fitz.Matrix(ctm.a, ctm.b, ctm.c, ctm.d, ctm.e, ctm.f)
    points = [fitz.Point(p) * matrix for p in walker.points]
    if len(points) == 5 and (points[0] == points[-1] if exact else abs(points[0] - points[-1]) < .01):
        points.pop()
    if not walker.valid or len(points) != 4:
        return False
    if exact:
        return (not walker.closed_early
                and len({p.x for p in points}) == len({p.y for p in points}) == 2
                and len({(p.x, p.y) for p in points}) == 4
                and all(a.x == b.x or a.y == b.y for a, b in zip(points, points[1:] + points[:1])))
    if len({round(p.x, 2) for p in points}) != 2 or len({round(p.y, 2) for p in points}) != 2:
        return False
    return all(abs(a.x - b.x) < .01 or abs(a.y - b.y) < .01 for a, b in zip(points, points[1:] + points[:1]))


class PaintDevice(m.FzDevice2):
    def __init__(self, page_rect, target=None, selected=None):
        super().__init__()
        self.target, self.selected = target, selected
        self.count = 0
        self.clips = [(fitz.Rect(page_rect), True, target is None)]
        self.clip_alphas = [1.0]
        self.masks = []
        self.mask_records = []
        self.group_stack = []
        self.tile_stack = []
        self.groups = []
        self.states = []
        for name in PAINT | STATE:
            getattr(self, "use_virtual_" + name)()

    def _call(self, name, args):
        include = True
        for mask in self.mask_records:
            mask.observe(name, args, *self.clips[-1][:2], _rectangular_clip)
        if name in PAINT:
            if self.count % 256 == 0:
                check_cancelled()
            building_mask = bool(self.masks)
            clip, simple, exact = self.clips[-1]
            if self.target is None:
                native_text = True
                if name in {"fill_text", "stroke_text"}:
                    # Stroke callbacks insert fz_stroke_state before the CTM.
                    native_text = _native_text_matrix(args[0], args[2 if name == "stroke_text" else 1])
                uniform = self.clip_alphas[-1] if not building_mask and not simple else None
                self.states.append(PaintState(fitz.Rect(clip), simple and not building_mask, building_mask,
                                              native_text, uniform, exact and not building_mask))
            include = self.selected is None or self.count in self.selected or building_mask
            self.count += 1
        elif name.startswith("clip_"):
            previous, simple, exact = self.clips[-1]
            if name == "clip_path":
                path, _, ctm, _ = args
                bounds = fitz.Rect(fitz.JM_py_from_rect(m.ll_fz_bound_path(path, None, ctm)))
                rectangular = _rectangular_clip(path, ctm)
                self.clips.append((previous & bounds, simple and rectangular,
                                   exact and rectangular and _rectangular_clip(path, ctm, exact=True)))
                self.clip_alphas.append(self.clip_alphas[-1] if rectangular else None)
            else:
                self.clips.append((previous, False, False))
                self.clip_alphas.append(None)
        elif name == "pop_clip":
            if len(self.clips) > 1:
                self.clips.pop()
                self.clip_alphas.pop()
        elif name == "begin_mask":
            from magic_converter.core.pdf.pdf_masks import UniformMask
            self.mask_records.append(UniformMask(args, self.clip_alphas[-1], bool(self.masks)))
            self.masks.append(len(self.clips))
        elif name == "end_mask":
            mask = self.mask_records.pop() if self.mask_records else None
            if self.masks:
                self.masks.pop()
            alpha = mask.alpha(args) if mask else None
            self.clips.append((self.clips[-1][0] & mask.area if alpha is not None else self.clips[-1][0], False, False))
            self.clip_alphas.append(alpha)
        elif name == "begin_group":
            area, _, isolated, knockout, blendmode, alpha = args
            special = knockout or blendmode != 0 or alpha < .999
            # Child compositing uses its parent's accumulated backdrop. An isolated
            # ancestor starts with transparency, so page ink before it cannot
            # contribute. The child's own isolation does not isolate the blend
            # used when compositing that child into its parent.
            floor = next((g[0] for g in reversed(self.group_stack) if g[4]), 0)
            self.group_stack.append((self.count, fitz.Rect(fitz.JM_py_from_rect(area)),
                                     special, blendmode != 0, isolated, floor))
        elif name == "end_group":
            if self.group_stack:
                start, bounds, special, backdrop, _, floor = self.group_stack.pop()
                if special and self.count > start and self.target is None:
                    self.groups.append(PaintGroup(start, self.count, bounds, backdrop, floor))
        elif name == "begin_tile":
            # Pattern tiles are indivisible paint groups; their paths must not
            # be promoted to free-floating slide objects.
            self.clips.append((self.clips[-1][0], False, False))
            self.clip_alphas.append(None)
            bounds = fitz.Rect(fitz.JM_py_from_rect(m.ll_fz_transform_rect(args[0], args[4])))
            self.tile_stack.append((self.count, bounds))
        elif name == "end_tile":
            if len(self.clips) > 1:
                self.clips.pop()
                self.clip_alphas.pop()
            start, bounds = self.tile_stack.pop()
            if self.target is None and self.count > start:
                self.groups.append(PaintGroup(start, self.count, bounds, False))
        if self.target is not None and include:
            if name == "begin_tile":
                # Disable tile caching: a cache hit would skip callbacks and
                # invalidate the sequence numbers used by the filter.
                return m.ll_fz_begin_tile_tid(self.target.m_internal, *args[:-2], 0, 0)
            # The high-level ll_fz_fill_text wrapper expects a Python colour
            # tuple. Device callbacks supply a native float*, so forward it
            # directly to the underlying generated binding.
            if name == "fill_text":
                return m._mupdf.ll_fz_fill_text(self.target.m_internal, *args)
            if name == "end_mask":
                return m.ll_fz_end_mask_tr(self.target.m_internal, *args)
            return getattr(m, "ll_fz_" + name)(self.target.m_internal, *args)
        if name == "begin_tile":
            return 0


def _method(name):
    def callback(self, context, *args):
        return self._call(name, args)
    return callback


for _name in PAINT | STATE:
    setattr(PaintDevice, _name, _method(_name))


class PdfPaint:
    def __init__(self, page):
        self.rect = page.rect
        self.log = page.get_bboxlog()
        self._page = None
        self.display = page.get_displaylist(annots=True)
        device = PaintDevice(self.rect)
        self._run(device, fitz.Matrix(1, 1))
        if device.count != len(self.log) and len(self.log) <= MAX_PAGE_RETRY_PAINTS:
            # MuPDF's display-list optimiser drops fully clipped text/shades,
            # while bbox/drawing/text-trace extraction runs the original page.
            # Retry that same page interpreter for inspection AND every replay;
            # deleting/reindexing bbox entries would misidentify native objects.
            self._page = page
            device = PaintDevice(self.rect)
            self._run(device, fitz.Matrix(1, 1))
        if device.count != len(self.log):
            raise PaintSequenceMismatch("PDF paint sequence does not match extracted geometry")
        self.states = device.states
        self.groups = self._atomic_groups(device.groups)

    def _atomic_groups(self, groups):
        """Keep a blend and its contributing backdrop in one replacement.

        Replaying the backdrop inside a picture while also leaving its native
        objects below that picture applies translucent ink twice. Expand only
        the connected paint interval; unrelated earlier/later objects remain
        editable. Include complete enclosing groups before removing any paint.
        """
        expanded = []
        for group in groups:
            start, end, bounds = group.start, group.end, fitz.Rect(group.bounds)
            if group.backdrop:
                # Form BBoxes often cover the whole page, including empty
                # space. Only painted ink can contribute to this backdrop.
                bounds = fitz.Rect()
                for sequence in range(start, end):
                    bounds |= self.visible_bounds(sequence)
                previous = None
                while previous != (start, end, tuple(bounds)):
                    previous = start, end, tuple(bounds)
                    for other in groups:
                        if other.start < end and other.end > start:
                            start, end = min(start, other.start), max(end, other.end)
                            for sequence in range(other.start, other.end):
                                bounds |= self.visible_bounds(sequence)
                    for sequence in range(start, end):
                        bounds |= self.visible_bounds(sequence)
                    for sequence in range(start - 1, group.backdrop_start - 1, -1):
                        visible = self.visible_bounds(sequence)
                        if not visible.is_empty and bounds.intersects(visible):
                            for included in range(sequence, start):
                                bounds |= self.visible_bounds(included)
                            start = sequence
            expanded.append(PaintGroup(start, end, bounds, False))
        result = []
        for group in sorted(expanded, key=lambda item: (item.start, -item.end)):
            if result and group.start < result[-1].end:
                result[-1].end = max(result[-1].end, group.end)
                result[-1].bounds |= group.bounds
            else:
                result.append(group)
        return result

    def _run(self, device, matrix):
        if self._page is not None:
            m.fz_run_page(self._page.this, device, fitz.JM_matrix_from_py(matrix), m.FzCookie())
            check_cancelled()
            return
        # A crop scissor can cull display-list calls, changing their sequence
        # numbers. The target pixmap clips output; replay the entire list.
        m.fz_run_display_list(self.display.this, device, fitz.JM_matrix_from_py(matrix),
                              m.FzRect(m.FzRect.Fixed_INFINITE), m.FzCookie())
        check_cancelled()

    def visible_bounds(self, sequence):
        return fitz.Rect(self.log[sequence][1]) & self.states[sequence].clip & self.rect

    def native_safe(self, sequences, bounds, *, allow_uniform_mask=False):
        bounds = fitz.Rect(bounds)
        for sequence in sequences:
            state = self.states[sequence]
            supported = state.simple or allow_uniform_mask and state.mask_alpha is not None
            if not supported or not (state.clip + (-.3, -.3, .3, .3)).contains(bounds):
                return False
            if any(group.start <= sequence < group.end for group in self.groups):
                return False
        return True

    def render(self, sequences, bounds, dpi=200):
        bounds = (fitz.Rect(bounds) + (-.5, -.5, .5, .5)) & self.rect
        if bounds.is_empty:
            return None
        matrix = fitz.Matrix(dpi / 72, dpi / 72)
        area = (bounds * matrix).irect
        # Protect large mixed-size PDFs from huge intermediate allocations.
        if area.width * area.height > 40_000_000:
            factor = (40_000_000 / (area.width * area.height)) ** .5
            matrix = fitz.Matrix(dpi / 72 * factor, dpi / 72 * factor)
            area = (bounds * matrix).irect
        # The public empty-pixmap constructor supplies FzSeparations(0), a
        # non-null separation object. MuPDF then proofs ordinary RGB/gray
        # paints through CMYK, changing colours. Match DisplayList.get_pixmap:
        # a null separation pointer, RGB target, premultiplied alpha.
        native = m.fz_new_pixmap_with_bbox(fitz.csRGB.this, fitz.JM_irect_from_py(area),
                                         m.FzSeparations(), 1)
        pixmap = fitz.Pixmap('raw', native)
        pixmap.clear_with()
        target = m.fz_new_draw_device(fitz.JM_matrix_from_py(matrix), pixmap.this)
        device = PaintDevice(self.rect, target, set(sequences))
        self._run(device, fitz.Matrix(1, 1))
        m.fz_close_device(target)
        if device.count != len(self.log):
            raise ValueError("PDF paint sequence changed during isolated rendering")
        # Strip transparent margins. This also prevents invisible PDF content
        # from creating giant, empty selection boxes in PowerPoint.
        from PIL import Image
        import io
        # MuPDF alpha samples are premultiplied; its PNG writer unpremultiplies
        # them correctly. Use that output when constructing the cropped image.
        image = Image.open(io.BytesIO(pixmap.tobytes("png")))
        ink = image.getchannel("A").getbbox()
        if not ink:
            return None
        cropped = image.crop(ink)
        buffer = io.BytesIO()
        cropped.save(buffer, "PNG", dpi=(dpi, dpi))
        inverse = ~matrix
        placed = fitz.Rect(area.x0 + ink[0], area.y0 + ink[1], area.x0 + ink[2], area.y0 + ink[3]) * inverse
        return buffer.getvalue(), placed
