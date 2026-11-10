"""Bounded nonzero-to-parity topology for simple linear Type3 contours.

Office fills compound paths by parity. Retain only boundaries that change
nonzero coverage, without moving any original vertex. Ambiguous boundaries
must keep the complete PDF replay instead.
"""
from __future__ import annotations

import math

from magic_converter.core.jobs.job_control import check_cancelled


def linear_nonzero_items(items, max_checks):
    epsilon = 1e-6
    contours, vertices, original = [], [], []
    for item in items:
        check_cancelled()
        original.append(item)
        if item[0] == 'l':
            start, end = item[1:]
            if any(not math.isfinite(v) for point in (start, end) for v in point):
                raise ValueError('Nonfinite Type3 vertex')
            if vertices and abs(start - vertices[-1]) > epsilon:
                raise ValueError('Discontinuous Type3 contour')
            if not vertices:
                vertices.append(start)
            if abs(end - vertices[-1]) <= epsilon:
                raise ValueError('Degenerate Type3 edge')
            vertices.append(end)
        elif item[0] == 'close':
            if vertices and abs(vertices[-1] - vertices[0]) <= epsilon:
                vertices.pop()
            if len(vertices) < 3:
                raise ValueError('Degenerate Type3 contour')
            contours.append((vertices, original))
            vertices, original = [], []
        else:
            raise ValueError('Nonlinear Type3 topology')
    if original or not contours:
        raise ValueError('Unclosed Type3 contour')
    count = sum(len(points) for points, _ in contours)
    if count * count > max_checks:
        raise ValueError('Type3 topology comparison budget exceeded')

    def cross(a, b, c):
        value = (b.x - a.x) * (c.y - a.y) - (b.y - a.y) * (c.x - a.x)
        tolerance = epsilon * max(1, abs(b - a), abs(c - a))
        return 1 if value > tolerance else -1 if value < -tolerance else 0

    def on_segment(a, b, p):
        return (min(a.x, b.x) - epsilon <= p.x <= max(a.x, b.x) + epsilon
                and min(a.y, b.y) - epsilon <= p.y <= max(a.y, b.y) + epsilon)

    edges, directions = [], []
    for index, (points, _) in enumerate(contours):
        origin = points[0]
        area = sum((a.x - origin.x) * (b.y - origin.y) - (b.x - origin.x) * (a.y - origin.y)
                   for a, b in zip(points, points[1:] + points[:1]))
        if abs(area) <= epsilon * max(1, max(abs(p - origin) for p in points)):
            raise ValueError('Ambiguous Type3 area')
        directions.append(1 if area > 0 else -1)
        for n, a in enumerate(points):
            b, previous = points[(n + 1) % len(points)], points[n - 1]
            if cross(previous, a, b) == 0:
                incoming, outgoing = a - previous, b - a
                if incoming.x * outgoing.x + incoming.y * outgoing.y <= 0:
                    raise ValueError('Retraced Type3 edge')
            edges.append((index, n, a, b))
    for index, (contour, n, a, b) in enumerate(edges):
        check_cancelled()
        for other, k, c, d in edges[:index]:
            if contour == other and (abs(n - k) == 1 or abs(n - k) == len(contours[contour][0]) - 1):
                continue  # The common endpoint is the only allowed contact.
            ab_c, ab_d, cd_a, cd_b = cross(a, b, c), cross(a, b, d), cross(c, d, a), cross(c, d, b)
            if ((ab_c * ab_d < 0 and cd_a * cd_b < 0)
                    or (ab_c == 0 and on_segment(a, b, c)) or (ab_d == 0 and on_segment(a, b, d))
                    or (cd_a == 0 and on_segment(c, d, a)) or (cd_b == 0 and on_segment(c, d, b))):
                raise ValueError('Crossing or touching Type3 boundaries')

    def contains(points, p):
        inside = False
        for a, b in zip(points, points[1:] + points[:1]):
            if (a.y > p.y) != (b.y > p.y):
                if p.x < a.x + (p.y - a.y) * (b.x - a.x) / (b.y - a.y):
                    inside = not inside
        return inside

    result = []
    for index, (points, original) in enumerate(contours):
        check_cancelled()
        outside = sum(directions[n] for n, (other, _) in enumerate(contours)
                      if n != index and contains(other, points[0]))
        if (outside != 0) != (outside + directions[index] != 0):
            result.extend(original)
    return result
