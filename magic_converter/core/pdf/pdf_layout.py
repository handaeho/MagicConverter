"""Shared, local PDF geometry for editable Word and PowerPoint objects.

PDFs contain drawing instructions, not Office tables. Detect ruled grids and
copy their geometry while the page is alive; no TableFinder object escapes
the lock or survives a page mutation. Raster-only pages remain images.
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from itertools import groupby

import pymupdf as fitz

from magic_converter.core.jobs.job_control import check_cancelled, CancellableRLock

PYMUPDF_LOCK = CancellableRLock()
Bounds = tuple[float, float, float, float]


@dataclass(frozen=True)
class PdfCharacter:
    text: str
    origin: tuple[float, float]


@dataclass(frozen=True)
class PdfTextSpan:
    bbox: Bounds
    text: str
    size: float
    color: int
    flags: int
    font: str
    quad: tuple[tuple[float, float], ...]
    line: int = 0
    origin: tuple[float, float] = (0, 0)
    direction: tuple[float, float] = (1, 0)
    ascender: float = 1
    descender: float = -.25
    characters: tuple[PdfCharacter, ...] = ()
    writing_mode: int = 0
    alpha: int = 255


@dataclass(frozen=True)
class PdfPicture:
    bbox: Bounds
    png: bytes


@dataclass
class PdfCell:
    row: int
    col: int
    rowspan: int
    colspan: int
    bbox: Bounds
    spans: list[PdfTextSpan] = field(default_factory=list)
    pictures: list[PdfPicture] = field(default_factory=list)
    fill: str = "FFFFFF"
    # None retains the legacy uniform ruled-grid style. An explicit mapping
    # records real/missing borders of tables with open outside edges.
    borders: dict[str, tuple[str, float] | None] | None = None


@dataclass
class PdfTable:
    bbox: Bounds
    xs: list[float]
    ys: list[float]
    cells: list[PdfCell]
    border_color: str = "666666"
    border_width: float = .5

    @property
    def rows(self) -> int:
        return len(self.ys) - 1

    @property
    def cols(self) -> int:
        return len(self.xs) - 1


def rgb_hex(color) -> str:
    if color is None:
        return "FFFFFF"
    if len(color) == 1:
        color = color * 3
    elif len(color) == 4:
        c, m, y, k = color
        color = (1 - min(1, c + k), 1 - min(1, m + k), 1 - min(1, y + k))
    return "".join(f"{round(max(0, min(1, channel)) * 255):02X}" for channel in color[:3])


def font_name(pdf_font: str) -> str:
    from magic_converter.core.pdf.pdf_fonts import resolve_font
    face = resolve_font(pdf_font)
    if face is not None:
        return face.family
    name = pdf_font.split("+", 1)[-1]
    try:
        name = name.encode("latin-1").decode("utf-8")
    except (UnicodeEncodeError, UnicodeDecodeError):
        pass
    aliases = {"helvetica": "Arial", "arial": "Arial", "arialmt": "Arial",
               "timesroman": "Times New Roman", "timesnewromanpsmt": "Times New Roman",
               "timesnewromanps": "Times New Roman", "timesnewroman": "Times New Roman",
               "courier": "Courier New", "couriernewpsmt": "Courier New",
               "malgungothic": "Malgun Gothic", "malgungothicregular": "Malgun Gothic",
               "휴먼명조": "Batang", "hcrbatang": "Batang"}
    family = re.sub(r"(?:[-,](?:BoldItalic|BoldOblique|BoldMT|ItalicMT|Bold|Italic|Oblique|Regular))$",
                    "", name, flags=re.IGNORECASE)
    return aliases.get(family.replace("-", "").replace(" ", "").lower(), family)


def text_spans(page: fitz.Page) -> list[PdfTextSpan]:
    result = []
    line_number = 0
    flags = fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES
    for block in page.get_text("rawdict", flags=flags)["blocks"]:
        if block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            line_number += 1
            for span in line.get("spans", []):
                characters = tuple(PdfCharacter(c["c"], tuple(c["origin"])) for c in span.get("chars", []))
                text = "".join(c.text for c in characters)
                bounds = fitz.Rect(span["bbox"])
                if not text.strip() or bounds.is_empty or not math.isfinite(span["size"]) or span["size"] <= 0:
                    continue
                char_flags = span.get("char_flags")
                if span.get("alpha", 255) == 0 or (char_flags is not None and not (char_flags & 24)):
                    continue  # Keep invisible OCR layers invisible.
                quad = fitz.recover_quad(line.get("dir", (1, 0)), span)
                result.append(PdfTextSpan(tuple(bounds), text, float(span["size"]),
                                          int(span.get("color", 0)), int(span.get("flags", 0)),
                                          span.get("font", "Arial"),
                                          tuple((p.x, p.y) for p in (quad.ul, quad.ur, quad.ll, quad.lr)), line_number,
                                          tuple(span["origin"]), tuple(line.get("dir", (1, 0))),
                                          float(span.get("ascender", 1)), float(span.get("descender", -.25)),
                                          characters, int(line.get("wmode", 0)), int(span.get("alpha", 255))))
    return result


def lines(spans: list[PdfTextSpan]) -> list[list[PdfTextSpan]]:
    grouped = [sorted(group, key=lambda span: span.bbox[0]) for _, group in
               groupby(sorted(spans, key=lambda span: span.line), key=lambda span: span.line)]
    return sorted(grouped, key=lambda line: (min(span.bbox[1] for span in line), min(span.bbox[0] for span in line)))


def contains_center(outer: Bounds, inner: Bounds, tolerance: float = .3) -> bool:
    x, y = (inner[0] + inner[2]) / 2, (inner[1] + inner[3]) / 2
    return outer[0] - tolerance <= x <= outer[2] + tolerance and outer[1] - tolerance <= y <= outer[3] + tolerance


def _coordinates(values) -> list[float]:
    groups: list[list[float]] = []
    for value in sorted(values):
        if not groups or value - groups[-1][-1] > 1:
            groups.append([value])
        else:
            groups[-1].append(value)
    return [sum(group) / len(group) for group in groups]


def fill_at(bounds: Bounds, drawings: list, default: str = "FFFFFF") -> str:
    inset = fitz.Rect(bounds) + (1, 1, -1, -1)
    result = default
    for drawing in drawings:
        if drawing.get("fill") is None or drawing.get("fill_opacity", 1) < .9:
            continue
        rect = fitz.Rect(drawing["rect"]) + (-.5, -.5, .5, .5)
        # An irregular path's bounding rectangle is not a solid cell fill.
        if rect.contains(inset) and any(item[0] == "re" for item in drawing["items"]):
            result = rgb_hex(drawing["fill"])
    return result


def page_background(page: fitz.Page, drawings: list | None = None) -> str:
    return fill_at(tuple(page.rect), drawings if drawings is not None else page.get_drawings())


def _table_rules(drawings: list) -> tuple[list, list]:
    """Real rules, excluding the edges of filled layout panels and images."""
    horizontal, vertical = [], []
    for drawing in drawings:
        stroked = drawing.get("color") is not None and drawing.get("stroke_opacity", 1) > .5
        for item in drawing["items"]:
            edges = [(item[1], item[2])] if item[0] == "l" and stroked else []
            if item[0] == "re":
                rect = fitz.Rect(item[1])
                if stroked:
                    edges = [(rect.tl, rect.tr), (rect.tr, rect.br), (rect.br, rect.bl), (rect.bl, rect.tl)]
                elif drawing.get("fill") is not None and min(rect.width, rect.height) <= 3:
                    if rect.width > rect.height:
                        horizontal.append(((rect.y0 + rect.y1) / 2, rect.x0, rect.x1))
                    else:
                        vertical.append(((rect.x0 + rect.x1) / 2, rect.y0, rect.y1))
            for first, last in edges:
                if abs(first.y - last.y) <= 1:
                    horizontal.append(((first.y + last.y) / 2, min(first.x, last.x), max(first.x, last.x)))
                elif abs(first.x - last.x) <= 1:
                    vertical.append(((first.x + last.x) / 2, min(first.y, last.y), max(first.y, last.y)))
    return horizontal, vertical


def _rule_covers(rules: list, coordinate: float, start: float, end: float) -> bool:
    intervals = sorted((max(start, a), min(end, b)) for c, a, b in rules
                       if abs(c - coordinate) <= 2 and b > start and a < end)
    covered, last = 0.0, start
    for a, b in intervals:
        covered += max(0, b - max(a, last))
        last = max(last, b)
    return covered >= (end - start) * .9


def _ruled_cells(rectangles: list, rules: tuple[list, list]) -> bool:
    horizontal, vertical = rules
    return all(_rule_covers(horizontal, y0, x0, x1) and _rule_covers(horizontal, y1, x0, x1)
               and _rule_covers(vertical, x0, y0, y1) and _rule_covers(vertical, x1, y0, y1)
               for x0, y0, x1, y1 in rectangles)


def extract_tables(page: fitz.Page, spans: list[PdfTextSpan]) -> list[PdfTable]:
    """Prefer stroked rules: filled rectangles can be glyphs or shading.

    The generic line detector is a fallback for tables made of filled rules.
    Unruled text columns are deliberately not guessed to be a table.
    """
    check_cancelled()
    drawings = page.get_drawings()
    rules = _table_rules(drawings)
    result = _open_tables(drawings, spans, rules)
    for strategy in ("lines_strict", "lines"):
        finder = page.find_tables(strategy=strategy)
        for source in finder.tables:
            check_cancelled()
            if source.row_count < 2 or source.col_count < 2:
                continue
            if any(fitz.Rect(table.bbox).intersects(fitz.Rect(source.bbox)) for table in result):
                continue
            rectangles = [tuple(rect) for rect in source.cells if rect is not None]
            if not rectangles or not _ruled_cells(rectangles, rules):
                continue  # TableFinder can mistake an entire slide layout for a grid.
            xs = _coordinates(value for rect in rectangles for value in (rect[0], rect[2]))
            ys = _coordinates(value for rect in rectangles for value in (rect[1], rect[3]))
            if len(xs) < 3 or len(ys) < 3 or (len(xs) - 1) * (len(ys) - 1) > 10000:
                continue
            cells, covered, invalid = [], set(), False
            for bounds in sorted(set(rectangles), key=lambda rect: (rect[1], rect[0])):
                col, end_col = (min(range(len(xs)), key=lambda i: abs(xs[i] - x)) for x in (bounds[0], bounds[2]))
                row, end_row = (min(range(len(ys)), key=lambda i: abs(ys[i] - y)) for y in (bounds[1], bounds[3]))
                occupied = {(r, c) for r in range(row, end_row) for c in range(col, end_col)}
                if not occupied or covered.intersection(occupied):
                    invalid = True
                    break
                covered.update(occupied)
                cells.append(PdfCell(row, col, end_row - row, end_col - col, bounds,
                                     fill=fill_at(bounds, drawings, page_background(page, drawings))))
            if invalid:
                continue
            for row in range(len(ys) - 1):
                for col in range(len(xs) - 1):
                    if (row, col) not in covered:
                        bounds = (xs[col], ys[row], xs[col + 1], ys[row + 1])
                        cells.append(PdfCell(row, col, 1, 1, bounds, fill=fill_at(bounds, drawings)))
            bounds = (xs[0], ys[0], xs[-1], ys[-1])
            table_spans = [span for span in spans if contains_center(bounds, span.bbox)]
            for span in table_spans:
                ul, ur = span.quad[:2]
                if abs(ur[1] - ul[1]) > 1:
                    invalid = True  # Keep rotated text in oriented textboxes.
                    break
                area = max(.01, fitz.Rect(span.bbox).get_area())
                intersections = [cell for cell in cells if
                                 (fitz.Rect(cell.bbox) & fitz.Rect(span.bbox)).get_area() / area > .2]
                if len(intersections) > 1:
                    invalid = True  # A chart grid crossing text is not a cell grid.
                    break
                owner = next((cell for cell in cells if contains_center(cell.bbox, span.bbox, 0)), None)
                if owner is not None:
                    owner.spans.append(span)
            if invalid:
                continue
            strokes = [drawing for drawing in drawings if drawing.get("color") is not None and
                       fitz.Rect(bounds).intersects(fitz.Rect(drawing["rect"]) + (-1, -1, 1, 1))]
            border = strokes[0] if strokes else {}
            result.append(PdfTable(bounds, xs, ys, cells, rgb_hex(border.get("color", (0, 0, 0))),
                                   max(.25, min(2, float(border.get("width") or .5)))))
        if result:
            break
    if not result and not spans:
        result = _blank_grid(drawings)
    return sorted((table for table in result if _table_border_styles(table, drawings, rules)),
                  key=lambda table: (table.bbox[1], table.bbox[0]))


def _table_border_styles(table, drawings, rules):
    """Keep each real rule's colour/width; ambiguous effects stay as paths."""
    import math
    horizontal, vertical = [], []
    for drawing in drawings:
        check_cancelled()
        color = drawing.get('color')
        width = drawing.get('width', 0)
        stroke = color is not None
        if stroke:
            if (drawing.get('stroke_opacity', 1) != 1 or drawing.get('dashes', '[] 0') != '[] 0'
                    or not math.isfinite(width) or not .05 <= width <= 20):
                continue
            style = (rgb_hex(color), width)
        for item in drawing['items']:
            edges = [(item[1], item[2])] if item[0] == 'l' and stroke else []
            if item[0] == 're':
                rect = fitz.Rect(item[1])
                if stroke:
                    edges = [(rect.tl, rect.tr), (rect.tr, rect.br), (rect.br, rect.bl), (rect.bl, rect.tl)]
                elif drawing.get('fill') is not None and drawing.get('fill_opacity', 1) == 1:
                    fill = rgb_hex(drawing['fill'])
                    if .05 <= rect.height <= 3 and rect.width > rect.height:
                        horizontal.append(((rect.y0 + rect.y1) / 2, rect.x0, rect.x1, (fill, rect.height)))
                    elif .05 <= rect.width <= 3 and rect.height > rect.width:
                        vertical.append(((rect.x0 + rect.x1) / 2, rect.y0, rect.y1, (fill, rect.width)))
            for a, b in edges:
                if abs(a.y - b.y) < .1:
                    horizontal.append(((a.y + b.y) / 2, min(a.x, b.x), max(a.x, b.x), style))
                elif abs(a.x - b.x) < .1:
                    vertical.append(((a.x + b.x) / 2, min(a.y, b.y), max(a.y, b.y), style))
    if len(table.cells) * (len(horizontal) + len(vertical)) * 2 > 200000:
        return False
    for cell in table.cells:
        check_cancelled()
        x0, y0, x1, y1 = cell.bbox
        borders = {}
        for edge, styled, plain, coordinate, start, end in (
                ('left', vertical, rules[1], x0, y0, y1), ('right', vertical, rules[1], x1, y0, y1),
                ('top', horizontal, rules[0], y0, x0, x1), ('bottom', horizontal, rules[0], y1, x0, x1)):
            if not _rule_covers(plain, coordinate, start, end):
                borders[edge] = None
                continue
            matches = [(c, a, b, style) for c, a, b, style in styled
                       if abs(c - coordinate) < .1 and min(b, end) - max(a, start) > .1]
            styles = {s for _, _, _, s in matches}
            if len(styles) != 1 or not _rule_covers([r[:3] for r in matches], coordinate, start, end):
                return False
            borders[edge] = styles.pop()
        cell.borders = borders
    return True


def _open_tables(drawings, spans, rules):
    """Recognise repeated ruled cells when an outside border was omitted.

    The endpoints of at least three full-width rules establish the sides.
    Continuous internal column rules and repeated populated cells are required;
    aligned text alone, chart grids and presentation panels are insufficient.
    """
    horizontal, vertical = rules
    groups = []
    for rule in sorted(horizontal, key=lambda item:(item[1],item[2],item[0])):
        if rule[2]-rule[1]<40:
            continue
        owner=next((group for group in groups if abs(group[0][1]-rule[1])<1 and abs(group[0][2]-rule[2])<1),None)
        if owner is None:groups.append([rule])
        else:owner.append(rule)
    result=[]
    for group in groups:
        ys=_coordinates(rule[0] for rule in group)
        if len(ys)<3:continue
        left=sum(rule[1] for rule in group)/len(group)
        right=sum(rule[2] for rule in group)/len(group)
        columns=[rule for rule in vertical if left+1<rule[0]<right-1
                 and rule[1]<=ys[0]+1 and rule[2]>=ys[-1]-1]
        xs=_coordinates([left,right]+[rule[0] for rule in columns])
        if len(xs)<3 or len(xs)>65:continue
        starts=_coordinates(rule[1] for rule in columns)
        # A common earlier start reveals a header above the first horizontal
        # rule. It must be short and corroborated by every internal column.
        if len(starts)==1 and ys[0]-median_spacing(ys)*1.8<=starts[0]<ys[0]-2:
            ys.insert(0,starts[0])
        if len(ys)*len(xs)>10000:continue
        if not all(_rule_covers(horizontal,y,left,right) for y in ys[1:-1]):continue
        if not all(_rule_covers(vertical,x,ys[0],ys[-1]) for x in xs[1:-1]):continue
        closed=(_rule_covers(vertical,left,ys[0],ys[-1]) and _rule_covers(vertical,right,ys[0],ys[-1])
                and _rule_covers(horizontal,ys[0],left,right) and _rule_covers(horizontal,ys[-1],left,right))
        if closed:continue  # The standard finder handles merges in closed grids.
        bounds=(left,ys[0],right,ys[-1])
        cells=[PdfCell(row,col,1,1,(xs[col],ys[row],xs[col+1],ys[row+1]),
                       fill=fill_at((xs[col],ys[row],xs[col+1],ys[row+1]),drawings))
               for row in range(len(ys)-1) for col in range(len(xs)-1)]
        invalid=False
        for span in spans:
            if not contains_center(bounds,span.bbox):continue
            if abs(span.direction[1])>.01:
                invalid=True;break
            area=max(.01,fitz.Rect(span.bbox).get_area())
            intersections=[cell for cell in cells if (fitz.Rect(cell.bbox)&fitz.Rect(span.bbox)).get_area()/area>.2]
            if len(intersections)>1:
                invalid=True;break
            owner=next((cell for cell in cells if contains_center(cell.bbox,span.bbox,0)),None)
            if owner:owner.spans.append(span)
        if invalid:continue
        occupancy=[sum(bool(cell.spans) for cell in cells if cell.row==row) for row in range(len(ys)-1)]
        if min(occupancy,default=0)<2 or sum(occupancy)<len(cells)*.6:continue
        strokes=[d for d in drawings if d.get('color') is not None and fitz.Rect(bounds).intersects(fitz.Rect(d['rect'])+(-1,-1,1,1))]
        border=strokes[0] if strokes else {}
        color=rgb_hex(border.get('color',(0,0,0)))
        width=max(.25,min(2,float(border.get('width') or .5)))
        for cell in cells:
            x0,y0,x1,y1=cell.bbox
            cell.borders={edge:(color,width) if _rule_covers(source,coord,start,end) else None
                          for edge,source,coord,start,end in (
                              ('left',vertical,x0,y0,y1),('right',vertical,x1,y0,y1),
                              ('top',horizontal,y0,x0,x1),('bottom',horizontal,y1,x0,x1))}
        result.append(PdfTable(bounds,xs,ys,cells,color,width))
    return result


def median_spacing(values):
    from statistics import median
    return median(b-a for a,b in zip(values,values[1:]))


def _blank_grid(drawings: list) -> list[PdfTable]:
    """TableFinder drops text-free grids; retain a complete ruled blank grid."""
    vertical, horizontal = [], []
    for drawing in drawings:
        if drawing.get("color") is None:
            continue
        for item in drawing["items"]:
            edges = [(item[1], item[2])] if item[0] == "l" else []
            if item[0] == "re":
                rect = fitz.Rect(item[1])
                edges = [(rect.tl, rect.tr), (rect.tr, rect.br), (rect.br, rect.bl), (rect.bl, rect.tl)]
            for first, last in edges:
                if abs(first.x - last.x) < .5 and abs(first.y - last.y) > 10:
                    vertical.append((first.x, min(first.y, last.y), max(first.y, last.y)))
                if abs(first.y - last.y) < .5 and abs(first.x - last.x) > 10:
                    horizontal.append((first.y, min(first.x, last.x), max(first.x, last.x)))
    xs, ys = _coordinates(line[0] for line in vertical), _coordinates(line[0] for line in horizontal)
    if len(xs) < 3 or len(ys) < 3 or (len(xs) - 1) * (len(ys) - 1) > 10000:
        return []
    if not all(any(abs(line[0] - x) < 1 and line[1] <= ys[0] + 1 and line[2] >= ys[-1] - 1
                   for line in vertical) for x in xs):
        return []
    if not all(any(abs(line[0] - y) < 1 and line[1] <= xs[0] + 1 and line[2] >= xs[-1] - 1
                   for line in horizontal) for y in ys):
        return []
    cells = [PdfCell(row, col, 1, 1, (xs[col], ys[row], xs[col + 1], ys[row + 1]))
             for row in range(len(ys) - 1) for col in range(len(xs) - 1)]
    return [PdfTable((xs[0], ys[0], xs[-1], ys[-1]), xs, ys, cells)]


def pictures(page: fitz.Page) -> list[PdfPicture]:
    """Copy each displayed image placement, including PDF crop/rotation/mask."""
    result = []
    seen = set()
    for info in page.get_image_info():
        check_cancelled()
        rect = fitz.Rect(info["bbox"]) & page.rect
        if rect.is_empty or rect.is_infinite or rect.width < .5 or rect.height < .5:
            continue
        identity = tuple(round(value, 2) for value in rect)
        if identity in seen:
            continue
        seen.add(identity)
        dpi = max(36, min(200, int(72 * math.sqrt(40_000_000 / max(1, rect.get_area())))))
        png = page.get_pixmap(clip=rect, dpi=dpi, alpha=False).tobytes("png")
        result.append(PdfPicture(tuple(rect), png))
    return result


def assign_pictures(tables: list[PdfTable], images: list[PdfPicture]) -> list[PdfPicture]:
    outside = []
    for image in images:
        owner = next((cell for table in tables for cell in table.cells if
                      fitz.Rect(cell.bbox).contains(fitz.Rect(image.bbox))), None)
        if owner is None:
            outside.append(image)
        else:
            owner.pictures.append(image)
            # Visible text painted on a picture is already in its rendered crop.
            owner.spans[:] = [span for span in owner.spans if not contains_center(image.bbox, span.bbox)]
    return outside


def cell_alignment(cell: PdfCell) -> str:
    if not cell.spans:
        return "left"
    cell_lines = lines(cell.spans)
    left, right = min(s.bbox[0] for s in cell.spans), max(s.bbox[2] for s in cell.spans)
    tolerance = max(2, (cell.bbox[2] - cell.bbox[0]) * .08)
    if len(cell_lines) > 1:
        starts = [min(s.bbox[0] for s in line) for line in cell_lines]
        ends = [max(s.bbox[2] for s in line) for line in cell_lines]
        centers = [(start + end) / 2 for start, end in zip(starts, ends)]
        if max(starts) - min(starts) < 2 and max(centers) - min(centers) > 3:
            return "left"
    if abs((left + right - cell.bbox[0] - cell.bbox[2]) / 2) <= tolerance:
        return "center"
    if cell.bbox[2] - right <= 4 and left - cell.bbox[0] > 8:
        return "right"
    return "left"


def cell_margins(cell: PdfCell) -> tuple[float, float, float, float]:
    if not cell.spans:
        return 2, 2, 1, 1
    width, height = cell.bbox[2] - cell.bbox[0], cell.bbox[3] - cell.bbox[1]
    left = max(1, min(width / 4, min(s.bbox[0] for s in cell.spans) - cell.bbox[0]))
    if cell_alignment(cell) == "center":
        left = 1
    return (left,
            1, max(0, min(height / 3, min(s.bbox[1] for s in cell.spans) - cell.bbox[1])), 0)
