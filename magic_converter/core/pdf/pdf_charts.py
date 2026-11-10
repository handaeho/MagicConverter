"""Conservative, PDF-only recognition of Cartesian charts.

No document names, page numbers, known years or domain-specific data are used.
Regular categories, a linear numeric axis and matching vector marks must agree.
An uncertain interpretation is left to the ordinary object-preserving converter.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from dataclasses import dataclass
from statistics import median

import pymupdf as fitz

from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.pdf.pdf_layout import Bounds, PdfTextSpan, rgb_hex, font_name


@dataclass
class ChartLabel:
    text: str
    bbox: Bounds
    indices: set[int]
    size: float
    font: str
    color: str
    bold: bool = False

    @property
    def center(self):
        r = fitz.Rect(self.bbox)
        return (r.tl + r.br) / 2


@dataclass
class ChartAxis:
    slope: float
    intercept: float
    minimum: float
    maximum: float
    major: float
    labels: list[ChartLabel]
    number_format: str

    def value(self, coordinate):
        return self.slope * coordinate + self.intercept

    def position(self, value):
        return (value - self.intercept) / self.slope


@dataclass
class ChartSeries:
    name: str
    kind: str
    values: list[float]
    colors: list[str]
    labels: list[ChartLabel | None]
    estimated: list[bool]
    secondary: bool = False
    line_width: float = 1.5
    marker_size: float = 0
    marker_fill: str = 'FFFFFF'
    label_position: str = 'outEnd'
    number_format: str = '0'


@dataclass
class PdfChart:
    bbox: Bounds
    plot: Bounds
    categories: list[str]
    series: list[ChartSeries]
    primary: ChartAxis
    secondary: ChartAxis | None
    horizontal: bool
    labels: set[int]
    paths: set[int]
    font: ChartLabel
    bar_fraction: float = .6
    legend: Bounds | None = None
    grid_color: str | None = None
    grid_width: float = .5
    grid_dash: bool = False
    axis_color: str | None = None
    between_categories: bool = True
    category_font: ChartLabel | None = None
    legend_font: ChartLabel | None = None
    value_axis_color: str | None = None


def _number(text):
    text = text.strip().replace('\u2212', '-').replace('\u00a0', '').replace('\u202f', '')
    negative = text.startswith('(') and text.endswith(')')
    if negative:
        text = '-' + text[1:-1]
    if not re.fullmatch(r'[-+]?\d[\d,. ]*%?', text):
        return None
    percent = text.endswith('%')
    token = text.rstrip('%').replace(' ', '')
    if ',' in token:
        if re.fullmatch(r'[-+]?\d{1,3}(,\d{3})+(\.\d+)?', token):
            token = token.replace(',', '')
        elif re.fullmatch(r'[-+]?\d+,\d{1,2}', token):
            token = token.replace(',', '.')
        else:
            return None
    try:
        value = float(token) / (100 if percent else 1)
    except ValueError:
        return None
    return value if math.isfinite(value) else None


def _format(labels):
    texts = [label.text.strip() for label in labels]
    percent = any(t.endswith('%') for t in texts)
    decimals = max((len(re.search(r'[.,](\d+)%?$', t)[1])
                    for t in texts if re.search(r'\.\d+%?$', t)
                    or re.search(r',\d{1,2}%?$', t)), default=0)
    grouped = any(re.search(r'\d[, \u00a0\u202f]\d{3}(?:\D|$)', t) for t in texts)
    return ('#,##0' if grouped and not percent else '0') + ('.' + '0' * decimals if decimals else '') + ('%' if percent else '')


def _labels(spans):
    groups = defaultdict(list)
    for i, span in enumerate(spans):
        if span.writing_mode or abs(span.direction[1]) > .01:
            continue
        groups[span.line].append((i, span))
    result = []
    for group in groups.values():
        current = []
        for item in sorted(group, key=lambda item: item[1].bbox[0]):
            if current and item[1].bbox[0] - current[-1][1].bbox[2] > max(item[1].size, current[-1][1].size) * .8:
                result.append(_label(current))
                current = []
            current.append(item)
        if current:
            result.append(_label(current))
    return result


def _label(items):
    bounds = fitz.Rect(items[0][1].bbox)
    text = ''
    previous = None
    for _, span in items:
        if previous and span.bbox[0] - previous.bbox[2] > span.size * .15 and not text.endswith(' '):
            text += ' '
        text += span.text
        bounds |= fitz.Rect(span.bbox)
        previous = span
    style = max((s for _, s in items), key=lambda s: s.size)
    return ChartLabel(text.strip(), tuple(bounds), {i for i, _ in items}, style.size,
                      font_name(style.font), f'{style.color:06X}', bool(style.flags & 16))


def _cluster(items, key, tolerance=1):
    groups = []
    for item in sorted(items, key=key):
        if not groups or key(item) - key(groups[-1][-1]) > tolerance:
            groups.append([item])
        else:
            groups[-1].append(item)
    return groups


def _regular(values):
    differences = [b-a for a, b in zip(values, values[1:])]
    return bool(differences) and min(differences) > 2 and max(differences) - min(differences) <= max(1.5, median(differences) * .06)


def _overlapping_columns(labels, horizontal):
    groups=[]
    for label in sorted(labels,key=lambda l:l.bbox[1] if horizontal else l.bbox[0]):
        start,end=(label.bbox[1],label.bbox[3]) if horizontal else (label.bbox[0],label.bbox[2])
        owner=next((g for g in groups if min(end,g[2])-max(start,g[1])>1),None)
        if owner is None:groups.append([[label],start,end])
        else:
            owner[0].append(label);owner[1]=max(owner[1],start);owner[2]=min(owner[2],end)
    return [g[0] for g in groups]


def _axes(labels, horizontal, u0, u1, v0, v1):
    """At least three aligned, monotonic ticks must define one linear scale."""
    candidates = []
    for side in (0, 1):
        selected = [label for label in labels if _number(label.text) is not None and
                    (u0 - 60 <= label.center.y <= u1 + 60 if horizontal else True) and
                    ((label.bbox[1] >= u1 + 2 if side == 0 else label.bbox[3] <= u0 - 2) if horizontal else
                     (label.bbox[2] <= u0 - 2 if side == 0 else label.bbox[0] >= u1 + 2))]
        edge = (lambda l: l.bbox[1] if side == 0 else l.bbox[3]) if horizontal else (lambda l: l.bbox[2] if side == 0 else l.bbox[0])
        center = (lambda l: l.center.y) if horizontal else (lambda l: l.center.x)
        groups = _cluster(selected, edge, 2) + _cluster(selected, center, 2) + _overlapping_columns(selected,horizontal)
        seen = set()
        for group in groups:
            identity = frozenset(i for label in group for i in label.indices)
            if identity in seen:
                continue
            seen.add(identity)
            group.sort(key=lambda l: l.center.x if horizontal else l.center.y)
            # Axis labels must be near the actual marks, not another column of numbers.
            distance = abs(median(edge(l) for l in group) - (u1 if horizontal and side == 0 else u0 if horizontal else u0 if side == 0 else u1))
            if len(group) < 3 or distance > 40:
                continue
            coords = [l.center.x if horizontal else l.center.y for l in group]
            values = [_number(l.text) for l in group]
            deltas = [b-a for a,b in zip(values, values[1:])]
            if min(coords) > v0 + 1.5 or max(coords) < v1 - 1.5 or not (all(d > 0 for d in deltas) or all(d < 0 for d in deltas)):
                continue
            avg_x, avg_y = sum(coords)/len(coords), sum(values)/len(values)
            denom = sum((x-avg_x)**2 for x in coords)
            if not denom:
                continue
            slope = sum((x-avg_x)*(y-avg_y) for x,y in zip(coords,values))/denom
            if (horizontal and slope<=0) or (not horizontal and slope>=0):
                continue
            intercept = avg_y - slope*avg_x
            major = min(abs(d) for d in deltas)
            if not major or max(abs(slope*x+intercept-y) for x,y in zip(coords,values)) > major*.035:
                continue  # Logarithmic, broken or ambiguous axes stay as PDF objects.
            candidates.append((side, ChartAxis(slope, intercept, min(values), max(values), major, group, _format(group))))
    return sorted(candidates, key=lambda item: (item[0], -len(item[1].labels)))


def _drawing_rectangles(drawing):
    result=[]
    segments=[]
    def finish():
        if not segments:return True
        points=[segments[0][0]]+[b for a,b in segments]
        r=fitz.Rect(min(p.x for p in points),min(p.y for p in points),max(p.x for p in points),max(p.y for p in points))
        corners={(round(p.x,2),round(p.y,2)) for p in (r.tl,r.tr,r.br,r.bl)}
        if {(round(p.x,2),round(p.y,2)) for p in points}!=corners:return False
        if any(abs(a.x-b.x)>.05 and abs(a.y-b.y)>.05 for a,b in segments):return False
        result.append(r);segments.clear();return True
    for item in drawing['items']:
        if item[0]=='re':
            if not finish():return None
            result.append(fitz.Rect(item[1]))
        elif item[0]=='qu':
            if not finish():return None
            q=fitz.Quad(item[1]);r=q.rect
            if any(min(abs(p.x-r.x0),abs(p.x-r.x1))>.05 or min(abs(p.y-r.y0),abs(p.y-r.y1))>.05 for p in (q.ul,q.ur,q.ll,q.lr)):return None
            result.append(r)
        elif item[0]=='l':
            a,b=item[1:]
            if segments and abs(a-segments[-1][1])>.05 and not finish():return None
            segments.append((a,b))
        else:return None
    return result if finish() else None


def _rectangles(drawings):
    rectangles = []
    for d in drawings:
        if d.get('fill') is None or d.get('fill_opacity',1) < .99 or not d['items']:
            continue
        if min(d['fill']) > .96:
            continue
        for r in _drawing_rectangles(d) or ():
            if min(r.width,r.height) > 1:
                rectangles.append((r,d))
    return rectangles


def _polyline(d):
    if d.get('color') is None or d.get('fill') is not None or len(d['items']) < 2 or any(i[0] != 'l' for i in d['items']):
        return None
    points = [d['items'][0][1]]
    for _, a, b in d['items']:
        if abs(a-points[-1]) > .4:
            return None
        points.append(b)
    if len(points) >= 3 and _regular([p.x for p in points]) and max(p.y for p in points)-min(p.y for p in points) > 2:
        return points
    return None


def _categories(labels, centers, baseline, horizontal):
    pitch = median(b-a for a,b in zip(centers,centers[1:]))
    per_category = []
    for center in centers:
        if horizontal:
            candidates = [l for l in labels if l.bbox[2] <= baseline-2 and baseline-l.bbox[2] <= 120
                          and abs(l.center.y-center) < pitch*.35]
            if candidates:
                right = max(l.bbox[2] for l in candidates)
                candidates = [l for l in candidates if right-l.bbox[2] < 10]
        else:
            candidates = [l for l in labels if baseline+1 <= l.bbox[1] <= baseline+min(85,pitch*1.2)
                          and abs(l.center.x-center) < pitch*.4 and fitz.Rect(l.bbox).width < pitch*1.5]
        if not candidates:
            return None
        candidates.sort(key=lambda l:(l.bbox[1],l.bbox[0]))
        first = candidates[0]
        chosen = [first]
        for label in candidates[1:]:
            if len(chosen) >= 4 or label.bbox[1] - chosen[-1].bbox[3] > first.size*.65:
                break
            chosen.append(label)
        per_category.append(chosen)
    if not horizontal and max(g[0].bbox[1] for g in per_category)-min(g[0].bbox[1] for g in per_category) > 3:
        return None
    return ['\n'.join(l.text for l in g) for g in per_category], [l for g in per_category for l in g]


def _value_label(labels, point, axis, pitch, horizontal, used):
    estimate = axis.value(point.x if horizontal else point.y)
    tolerance = max(abs(axis.slope)*2, axis.major*.06)
    candidates = []
    for label in labels:
        if label.indices & used or _number(label.text) is None:
            continue
        transverse = abs(label.center.y-point.y) if horizontal else abs(label.center.x-point.x)
        if transverse > pitch*.28:
            continue
        distance = abs(label.center.x-point.x) if horizontal else abs(label.center.y-point.y)
        if distance > max(20, label.size*2.5) and not (not horizontal and label.bbox[1] > point.y and label.bbox[3] < axis.position(axis.minimum)+1):
            continue
        value = _number(label.text)
        if abs(value-estimate) <= max(tolerance, abs(value)*.008):
            candidates.append((abs(value-estimate)/max(tolerance,1e-12) + distance/100, label, value))
    if candidates:
        _, label, value = min(candidates,key=lambda item:item[0])
        return value, label, False
    precision = max(0,min(6,math.ceil(-math.log10(max(abs(axis.slope)*.5,1e-8)))))
    return round(estimate,precision), None, True


def _path_sequences(d):
    return {d['seqno'],d['seqno']+1} if d['type']=='fs' else {d['seqno']}


def _same_rect(first,second):
    return max(abs(a-b) for a,b in zip(first,second))<.2


def _finish(labels, drawings, categories, category_labels, centers, plot, axes, series_marks, horizontal, bar_fraction):
    if any(not (fitz.Rect(plot)+(-1.5,-1.5,1.5,1.5)).contains(point)
           for _,points,_,_,_ in series_marks for point in points):
        return None
    primary = axes[0][1]
    secondary = None
    used = {i for label in category_labels + primary.labels for i in label.indices}
    selected_labels = list(category_labels)+primary.labels
    paths = set()
    series = []
    pitch = median(b-a for a,b in zip(centers,centers[1:]))
    for kind, points, marks, axis, is_secondary in series_marks:
        if is_secondary:
            secondary = axis
            used.update(i for label in axis.labels for i in label.indices)
            selected_labels += axis.labels
        values, data_labels, estimated = [], [], []
        for point in points:
            value, label, inferred = _value_label(labels,point,axis,pitch,horizontal,used)
            values.append(value); data_labels.append(label); estimated.append(inferred)
            if label:
                used.update(label.indices); selected_labels.append(label)
        colors = [rgb_hex(mark.get('fill') if kind=='bar' else mark.get('color')) for mark in marks]
        for mark in marks:
            paths.update(_path_sequences(mark))
        label_position = 'outEnd'
        if kind=='bar':
            baseline=axis.position(max(axis.minimum,min(0,axis.maximum)))
            relative=[]
            for label,point in zip(data_labels,points):
                if label is None:continue
                end=point.x if horizontal else point.y
                coordinate=label.center.x if horizontal else label.center.y
                if min(baseline,end)+1<coordinate<max(baseline,end)-1:
                    relative.append((coordinate-baseline)/(end-baseline))
            if relative:
                ratio=median(relative)
                label_position='inBase' if ratio<.34 else 'inEnd' if ratio>.66 else 'ctr'
        series.append(ChartSeries(f'Series {len(series)+1}',kind,values,colors,data_labels,estimated,is_secondary,
                                  marks[0].get('width') or 1.5,label_position=label_position,
                                  number_format=_format([l for l in data_labels if l]) if any(data_labels) else axis.number_format))
    r = fitz.Rect(plot)
    # Consume only recognised chart furniture. Unrelated captions remain objects.
    grid_color, grid_width, grid_dash = None,.5,False
    axis_color = None
    value_axis_color = None
    legend_labels = []
    for d in drawings:
        dr = fitz.Rect(d['rect'])
        if _path_sequences(d) <= paths:
            continue
        # A circle at an observed line point is a line-series marker.
        if d['items'] and all(i[0]=='c' for i in d['items']) and .5<dr.width<14 and .5<dr.height<14:
            for s, (_,points,_,_,_) in zip(series,series_marks):
                if s.kind=='line' and any(abs((dr.tl+dr.br)/2-point)<1 for point in points):
                    paths.update(_path_sequences(d)); s.marker_size=dr.width; s.marker_fill=rgb_hex(d.get('fill'))
        if d.get('color') is not None and all(i[0]=='l' for i in d['items']):
            vertical_boundary=all(abs(a.x-b.x)<.1 and min(abs(a.x-r.x0),abs(a.x-r.x1))<1.5
                                  and abs(min(a.y,b.y)-r.y0)<1.5 and abs(max(a.y,b.y)-r.y1)<1.5 for _,a,b in d['items'])
            horizontal_boundary=all(abs(a.y-b.y)<.1 and min(abs(a.y-r.y0),abs(a.y-r.y1))<1.5
                                    and abs(min(a.x,b.x)-r.x0)<1.5 and abs(max(a.x,b.x)-r.x1)<1.5 for _,a,b in d['items'])
            if vertical_boundary if not horizontal else horizontal_boundary:
                paths.update(_path_sequences(d));value_axis_color=rgb_hex(d['color'])
            is_grid = True
            for _,a,b in d['items']:
                if horizontal:
                    aligned = abs(a.x-b.x)<.1 and abs(min(a.y,b.y)-r.y0)<1.5 and abs(max(a.y,b.y)-r.y1)<1.5
                else:
                    aligned = abs(a.y-b.y)<.1 and abs(min(a.x,b.x)-r.x0)<1.5 and abs(max(a.x,b.x)-r.x1)<1.5
                if not aligned or not (r+(-1,-1,1,1)).contains(a) or not (r+(-1,-1,1,1)).contains(b):
                    is_grid=False;break
            if is_grid:
                paths.update(_path_sequences(d))
                # A baseline alone is an axis, not a gridline at every tick.
                boundary = all(abs((i[1].x if horizontal else i[1].y)-(r.x0 if horizontal else r.y1))<1.5 for i in d['items'])
                if boundary:
                    axis_color=rgb_hex(d['color'])
                else:
                    grid_color=rgb_hex(d['color']);grid_width=d.get('width') or .5;grid_dash='[]' not in (d.get('dashes') or '[]')
        # Match each legend key to a series by colour and the following text.
        if not horizontal and 2<dr.width<35 and dr.height<12 and r.y0-30<=dr.y0<=r.y0+30:
            for s in series:
                key_color = rgb_hex(d.get('fill') if s.kind=='bar' else d.get('color'))
                if key_color != s.colors[0]:
                    continue
                matches = [l for l in labels if (not l.indices&used or l in legend_labels and l.text == s.name) and _number(l.text) is None
                           and 0<=l.bbox[0]-dr.x1<max(18,l.size*2.5) and abs(l.center.y-dr.y0-dr.height/2)<6]
                if matches:
                    label=min(matches,key=lambda l:l.bbox[0]);s.name=label.text
                    used.update(label.indices);selected_labels.append(label);legend_labels.append(label)
                    paths.update(_path_sequences(d))
    # Refuse partially recognised stacks or unsupported extra plotted series.
    for d in drawings:
        if _path_sequences(d) & paths:
            continue
        dr=fitz.Rect(d['rect'])
        if (r+(-.5,-.5,.5,.5)).intersects(dr+(-.1,-.1,.1,.1)):
            rects=_drawing_rectangles(d)
            if d.get('fill') is not None and rects and len(rects)==1 and rects[0].contains(r):
                continue  # A plot/page background stays below the live chart.
            return None  # Do not strand unrecognised markers or annotations.
    bounds=fitz.Rect(r)
    for l in selected_labels:
        bounds|=fitz.Rect(l.bbox)
    bounds+=(-7,-7,7,7)
    selected_rects=[fitz.Rect(mark['_bar_rect']) for kind,_,marks,_,_ in series_marks if kind=='bar' for mark in marks]
    for drawing in drawings:
        if not _path_sequences(drawing)&paths:continue
        if not (bounds+(-1,-1,1,1)).contains(fitz.Rect(drawing['rect'])):return None
        if drawing.get('fill') is not None:
            parts=_drawing_rectangles(drawing)
            if parts and any(any(_same_rect(part,selected) for selected in selected_rects) for part in parts):
                if any(not any(_same_rect(part,selected) for selected in selected_rects) for part in parts):
                    return None  # A batched bar operator also paints outside marks.
    legend=None
    if legend_labels:
        lr=fitz.Rect(legend_labels[0].bbox)
        for l in legend_labels[1:]:lr|=fitz.Rect(l.bbox)
        legend=tuple(lr+(-25,-2,2,2))
    chart=PdfChart(tuple(bounds),tuple(r),categories,series,primary,secondary,horizontal,used,paths,
                   primary.labels[0],bar_fraction,legend,grid_color,grid_width,grid_dash,axis_color)
    chart.category_font=category_labels[0]
    chart.legend_font=legend_labels[0] if legend_labels else None
    chart.value_axis_color=value_axis_color
    return chart


def extract_charts(page, spans: list[PdfTextSpan], drawings=None) -> list[PdfChart]:
    """Recognise ordinary bars, grouped columns, lines and bar/line combinations.

    Raster charts, logarithmic axes, stacks and decorative diagrams are not
    guessed. Every chart is independent, including multiple charts on one page.
    """
    drawings=drawings if drawings is not None else page.get_drawings()
    labels=_labels(spans)
    rectangles=_rectangles(drawings)
    charts=[]
    for horizontal in (False,True):
        # Baseline membership is geometric, independent of colour or paint batching.
        endpoints=[(r.x0 if horizontal else r.y0,r,d) for r,d in rectangles]+[(r.x1 if horizontal else r.y1,r,d) for r,d in rectangles]
        for group in sorted(_cluster(endpoints,lambda t:t[0],.6),key=len,reverse=True):
            check_cancelled()
            if len(group)<3:continue
            baseline=median(t[0] for t in group)
            marks=[(r,d) for _,r,d in group if (r.height if horizontal else r.width)>2]
            widths=[r.height if horizontal else r.width for r,d in marks]
            if len(marks)<3 or max(widths)>min(widths)*1.12:continue
            marks.sort(key=lambda t:t[0].y0 if horizontal else t[0].x0)
            positions=[(r.y0+r.y1)/2 if horizontal else (r.x0+r.x1)/2 for r,d in marks]
            if any(b-a<min(widths)*.9 for a,b in zip(positions,positions[1:])):continue
            if _regular(positions):
                clusters=[[(r,d)] for r,d in marks]
            else:
                gaps=[b-a for a,b in zip(positions,positions[1:])]
                if not gaps or max(gaps)<min(gaps)*1.5:continue
                clusters=_cluster(marks,lambda t:(t[0].y0+t[0].y1)/2 if horizontal else (t[0].x0+t[0].x1)/2,min(gaps)*1.2)
                if len(clusters)<3 or len({len(c) for c in clusters})!=1 or len(clusters[0])>6:continue
            centers=[sum((r.y0+r.y1)/2 if horizontal else (r.x0+r.x1)/2 for r,d in c)/len(c) for c in clusters]
            if not _regular(centers):continue
            pitch=median(b-a for a,b in zip(centers,centers[1:]))
            u0,u1=centers[0]-pitch/2,centers[-1]+pitch/2
            value_coords=[v for r,d in marks for v in ((r.x0,r.x1) if horizontal else (r.y0,r.y1))]
            axes=_axes(labels,horizontal,u0,u1,min(value_coords),max(value_coords))
            if not axes:continue
            axis=axes[0][1]
            # Anchor zero or a truncated scale boundary to the actual baseline.
            nearest=min((axis.minimum,axis.maximum,0),key=lambda v:abs(axis.position(v)-baseline))
            if abs(axis.position(nearest)-baseline)>2:continue
            axis.intercept=nearest-axis.slope*baseline
            low,high=sorted((axis.position(axis.minimum),axis.position(axis.maximum)))
            categories=_categories(labels,centers,low if horizontal else high,horizontal)
            if categories is None:continue
            texts,category_labels=categories
            plot=fitz.Rect(low,u0,high,u1) if horizontal else fitz.Rect(u0,low,u1,high)
            if any(plot.intersects(fitz.Rect(c.plot)) for c in charts):continue
            series_marks=[]
            for slot in range(len(clusters[0])):
                items=[c[slot] for c in clusters]
                points=[]
                for r,d in items:
                    end=max((r.x0,r.x1) if horizontal else (r.y0,r.y1),key=lambda v:abs(v-baseline))
                    points.append(fitz.Point(end,(r.y0+r.y1)/2) if horizontal else fitz.Point((r.x0+r.x1)/2,end))
                series_marks.append(('bar',points,[dict(d,_bar_rect=tuple(r)) for r,d in items],axis,False))
            if not horizontal:
                for d in drawings:
                    points=_polyline(d)
                    if not points or len(points)!=len(centers) or any(abs(p.x-x)>pitch*.08 for p,x in zip(points,centers)):continue
                    if any(not (plot+(-2,-2,2,2)).contains(p) for p in points):continue
                    scored=[]
                    for side,a in axes:
                        # Bring each value-axis baseline onto the same plot edge.
                        anchor=min((a.minimum,a.maximum,0),key=lambda v:abs(a.position(v)-baseline))
                        if abs(a.position(anchor)-baseline)<=2:a.intercept=anchor-a.slope*baseline
                        score=sum(_value_label(labels,p,a,pitch,False,set())[1] is not None for p in points)
                        scored.append((score,a))
                    scored.sort(key=lambda item:item[0],reverse=True)
                    if len(scored)>1 and (scored[0][0]<len(points)*.6 or scored[0][0]==scored[1][0]):continue
                    a=scored[0][1]
                    series_marks.append(('line',points,[d]*len(points),a,a is not axis))
            fraction=(max(r.y1 for r,d in clusters[0])-min(r.y0 for r,d in clusters[0]) if horizontal else max(r.x1 for r,d in clusters[0])-min(r.x0 for r,d in clusters[0]))/pitch
            chart=_finish(labels,drawings,texts,category_labels,centers,plot,axes,series_marks,horizontal,fraction)
            if chart:charts.append(chart)
    # Line-only charts use the observed category point positions, not a bar baseline.
    for d in drawings:
        points=_polyline(d)
        if not points:continue
        centers=[p.x for p in points];pitch=median(b-a for a,b in zip(centers,centers[1:]))
        axes=_axes(labels,False,centers[0],centers[-1],min(p.y for p in points),max(p.y for p in points))
        if not axes:continue
        axis=axes[0][1]
        bottom=axis.position(axis.minimum)
        categories=_categories(labels,centers,bottom,False)
        if categories is None:continue
        texts,category_labels=categories
        plot=fitz.Rect(centers[0],axis.position(axis.maximum),centers[-1],bottom)
        if any(plot.intersects(fitz.Rect(c.plot)) for c in charts):continue
        marks=[]
        for line in drawings:
            ps=_polyline(line)
            if ps and len(ps)==len(points) and all(abs(p.x-q.x)<.5 for p,q in zip(ps,points)) and all((plot+(-1,-1,1,1)).contains(p) for p in ps):
                marks.append(('line',ps,[line]*len(ps),axis,False))
        chart=_finish(labels,drawings,texts,category_labels,centers,plot,axes,marks,False,0)
        if chart:
            chart.between_categories=False
            charts.append(chart)
    return charts
