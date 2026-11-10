"""Clip a proved convex cubic contour against exact rectangle edges."""
from __future__ import annotations

import pymupdf as fitz

from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.pdf.pdf_type3_curves import convex_nonzero_items


def _split(points, fraction):
    a, b, c, d = points
    ab, bc, cd = (a + (b - a) * fraction, b + (c - b) * fraction, c + (d - c) * fraction)
    abc, bcd = ab + (bc - ab) * fraction, bc + (cd - bc) * fraction
    crossing = abc + (bcd - abc) * fraction
    return (a, ab, abc, crossing), (crossing, bcd, cd, d)


def _clip_edge(segments, axis, boundary, inward):
    kept = []
    crossings = 0
    for item in segments:
        check_cancelled()
        points = item[1:]
        distances = tuple(inward * (p[axis] - boundary) for p in points)
        if min(distances) >= 0:
            kept.append(item)
            continue
        if max(distances) <= 0:
            continue
        entering = distances[0] < 0 < distances[-1]
        leaving = distances[-1] < 0 < distances[0]
        if not ((entering and list(distances) == sorted(distances))
                or (leaving and list(distances) == sorted(distances, reverse=True))):
            raise ValueError('Cubic clip crossing is not strictly bracketed and monotone')
        crossings += 1
        if item[0] == 'l':
            fraction = distances[0] / (distances[0] - distances[-1])
            point = points[0] + (points[-1] - points[0]) * fraction
            left, right = (points[0], point), (point, points[-1])
        else:
            low, high = 0.0, 1.0
            for _ in range(40):
                fraction = (low + high) / 2
                left, right = _split(points, fraction)
                if (inward * (left[-1][axis] - boundary) > 0) == entering:
                    high = fraction
                else:
                    low = fraction
            point = left[-1]
        if abs(point[axis] - boundary) > 1e-5:
            raise ValueError('Cubic clip crossing precision unavailable')
        cut = fitz.Point(boundary, point.y) if axis == 0 else fitz.Point(point.x, boundary)
        kept.append((item[0], cut, *right[1:]) if entering else (item[0], *left[:-1], cut))

    gaps = [index for index, (left, right) in enumerate(zip(kept, kept[1:] + kept[:1]))
            if left[-1] != right[1]]
    if crossings != 2 or len(gaps) != 1:
        raise ValueError('Cubic clip needs one retained arc and one closing edge')
    split = gaps[0] + 1
    kept = kept[split:] + kept[:split]
    if abs(kept[-1][-1] - kept[0][1]) <= .01:
        raise ValueError('Cubic clip closure is too short')
    return kept + [('l', kept[-1][-1], kept[0][1])]


def clip_cubic_glyph(items, clip, budget, checks):
    """Keep the original cubic pieces through one or two adjacent half-planes."""
    if not (3 <= len(items) <= 17 and items[-1] == ('close',)
            and all(item[0] == 'c' for item in items[:-1])):
        raise ValueError('Partial curved clip needs one closed cubic contour')
    segments = items[:-1]
    if any(left[-1] != right[1] for left, right in zip(segments, segments[1:] + segments[:1])):
        raise ValueError('Discontinuous cubic clip contour')
    edges = [(axis, boundary, inward) for axis, boundary, inward in
             ((0, clip.x0, 1), (0, clip.x1, -1), (1, clip.y0, 1), (1, clip.y1, -1))
             if any(inward * (p[axis] - boundary) < 0 for item in segments for p in item[1:])]
    if len(edges) not in (1, 2) or (len(edges) == 2 and edges[0][0] == edges[1][0]):
        raise ValueError('Cubic clip needs one edge or two adjacent edges')
    proof_cost = (3 * len(segments)) ** 2
    checks[0] -= proof_cost + 80 * len(edges)
    if checks[0] < 0 or convex_nonzero_items(items, proof_cost) != items:
        raise ValueError('Cubic clip proof unavailable within budget')
    for edge in edges:
        segments = _clip_edge(segments, *edge)
    kept = segments[:-1]
    if (not any(item[0] == 'c' for item in kept)
            or any(not (clip.x0 <= p.x <= clip.x1 and clip.y0 <= p.y <= clip.y1)
                   for item in kept for p in item[1:])):
        raise ValueError('Cubic clip geometry is outside its proof bounds')
    budget[0] -= sum(len(item) - 2 for item in kept) + 1
    if budget[0] < 0:
        raise ValueError('Clipped cubic command budget exceeded')
    return kept + [('close',)]
