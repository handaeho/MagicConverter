"""Conservative topology for compound convex Bezier Type3 contours.

A convex Bezier contour contains its endpoint polygon and is contained in
its control polygon. These bounds prove separation/nesting without flattening
or solving approximate curve intersections. Inconclusive cases keep PDF replay.
"""
from __future__ import annotations

import math

from magic_converter.core.jobs.job_control import check_cancelled


def convex_nonzero_items(items, max_checks):
    epsilon = 1e-6
    contours, controls, endpoints, original = [], [], [], []
    for item in items:
        check_cancelled()
        original.append(item)
        if item[0] in {'l', 'c'}:
            points = item[1:]
            if len(points) != (2 if item[0] == 'l' else 4):
                raise ValueError('Invalid Type3 curve')
            if any(not math.isfinite(v) for p in points for v in p):
                raise ValueError('Nonfinite Type3 curve')
            if endpoints and abs(points[0] - endpoints[-1]) > epsilon:
                raise ValueError('Discontinuous Type3 curve')
            if not endpoints:
                endpoints.append(points[0])
            endpoints.append(points[-1])
            for point in points:
                if not controls or abs(point - controls[-1]) > epsilon:
                    controls.append(point)
        elif item[0] == 'close':
            if controls and abs(controls[-1] - controls[0]) <= epsilon:
                controls.pop()
            if endpoints and abs(endpoints[-1] - endpoints[0]) <= epsilon:
                endpoints.pop()
            contours.append((controls, endpoints, original))
            controls, endpoints, original = [], [], []
        else:
            raise ValueError('Unsupported Type3 curve command')
    if original or not contours or sum(len(c) for c, _, _ in contours) ** 2 > max_checks:
        raise ValueError('Unclosed Type3 curve or comparison budget exceeded')

    def cross(a, b, p):
        value = (b.x - a.x) * (p.y - a.y) - (b.y - a.y) * (p.x - a.x)
        tolerance = epsilon * max(1, abs(b - a), abs(p - a))
        return 1 if value > tolerance else -1 if value < -tolerance else 0

    directions = []
    for polygon, chord, _ in contours:
        if len(polygon) < 3 or not chord:
            raise ValueError('Degenerate Type3 curve bounds')
        signs = set()
        for n, a in enumerate(polygon):
            check_cancelled()
            if any(abs(a - p) <= epsilon for p in polygon[:n]):
                raise ValueError('Repeated Type3 control point')
            b, previous = polygon[(n + 1) % len(polygon)], polygon[n - 1]
            if cross(previous, a, b) == 0:
                u, v = a - previous, b - a
                if u.x * v.x + u.y * v.y <= 0:
                    raise ValueError('Retraced Type3 control boundary')
            signs.update(sign for p in polygon if (sign := cross(a, b, p)))
            if len(signs) > 1:
                raise ValueError('Nonconvex Type3 control boundary')
        if len(signs) != 1:
            raise ValueError('Degenerate Type3 control boundary')
        directions.append(signs.pop())

    def inside(inner, outer, direction):
        if len(outer) < 3:
            return False  # A lens/closed cubic has no area-bearing chord bound.
        return all(cross(a, b, p) == direction for a, b in zip(outer, outer[1:] + outer[:1]) for p in inner)

    def separated(a, b):
        # A separating control-polygon edge proves disjoint convex hulls.
        for polygon, other in ((a, b), (b, a)):
            for start, end in zip(polygon, polygon[1:] + polygon[:1]):
                side = {cross(start, end, p) for p in polygon} - {0}
                if len(side) == 1 and all(cross(start, end, p) == -next(iter(side)) for p in other):
                    return True
        return False

    # De Casteljau tightens proof bounds only. Never replace the authored
    # cubics with these subdivided segments or an approximate flattened path.
    def subdivide(original):
        result = []
        for item in original:
            check_cancelled()
            if item[0] != 'c':
                result.append(item)
                continue
            a, b, c, d = item[1:]
            ab, bc, cd = (a+b)/2, (b+c)/2, (c+d)/2
            abc, bcd = (ab+bc)/2, (bc+cd)/2
            middle = (abc+bcd)/2
            result.extend([('c', a, ab, abc, middle), ('c', middle, bcd, cd, d)])
        return result

    def bounds(original):
        hull, chord = [], []
        for item in original:
            if item[0] == 'close':
                continue
            if not chord:
                chord.append(item[1])
            chord.append(item[-1])
            for point in item[1:]:
                if not hull or abs(point-hull[-1]) > epsilon:
                    hull.append(point)
        if hull and abs(hull[-1]-hull[0]) <= epsilon:
            hull.pop()
        if chord and abs(chord[-1]-chord[0]) <= epsilon:
            chord.pop()
        return hull, chord, original

    proof, remaining = contours, max_checks
    for depth in range(5):
        cost = sum(len(hull) for hull, _, _ in proof) ** 2
        if cost > remaining:
            raise ValueError('Type3 refinement comparison budget exceeded')
        remaining -= cost
        ancestors, uncertain = [set() for _ in contours], False
        for n, (hull, chord, _) in enumerate(proof):
            check_cancelled()
            for k, (other_hull, other_chord, _) in enumerate(proof[:n]):
                if inside(hull, other_chord, directions[k]):
                    ancestors[n].add(k)
                elif inside(other_hull, chord, directions[n]):
                    ancestors[k].add(n)
                elif not separated(hull, other_hull):
                    uncertain = True
        if not uncertain:
            break
        if depth == 4:
            raise ValueError('Unproven Type3 curve separation or nesting')
        proof = [bounds(subdivide(original)) for _, _, original in proof]
    result = []
    for n, (_, _, original) in enumerate(contours):
        winding = sum(directions[k] for k in ancestors[n])
        if (winding != 0) != (winding + directions[n] != 0):
            result.extend(original)
    return result
