"""Locate text anchors in native PDF coordinates, without page rescaling."""
from statistics import median
import math
import re
import pymupdf as fitz
from magic_converter.core.jobs.job_control import check_cancelled


def _font_name_identity(name):
    """Decode reversible Korean PDF name bytes, without equating font aliases.

    A matching declaration is not proof of identical outlines or embedding.
    Keep raw names in the report and never feed this diagnostic into rendering.
    """
    name = re.sub(r'^[A-Z]{6}\+', '', name)
    try:
        raw = name.encode('latin-1')
    except UnicodeEncodeError:
        return name
    for encoding in ('utf-8', 'cp949'):
        try:
            decoded = raw.decode(encoding)
        except UnicodeDecodeError:
            continue
        if any('\uac00' <= c <= '\ud7a3' for c in decoded) and decoded.encode(encoding) == raw:
            return decoded
    return name


def _text_paint_states(page):
    """Identify explicit ignore/zero-alpha text, not clipping or occlusion."""
    try:
        traces = page.get_texttrace()
    except (AttributeError, ValueError, RuntimeError):
        return {}
    if sum(len(trace.get('chars', ())) for trace in traces) > 200_000:
        return {}  # Optional attribution stays unknown within its own budget.
    result = {}
    for trace in traces:
        check_cancelled()
        opacity = trace.get('opacity', 1)
        state = ('unknown' if not math.isfinite(opacity) else
                 'nonpainting' if trace.get('type') == 3 or opacity <= 0 else 'paint_requested')
        font = _font_name_identity(trace['font'])
        for index, (code, _, origin, _) in enumerate(trace['chars']):
            if index % 4096 == 0:
                check_cancelled()
            key = (code, round(origin[0], 3), round(origin[1], 3), font)
            # Simulated bold or coincident text may have multiple paints.
            if result.get(key) != 'paint_requested':
                result[key] = state
    return result


def page_text_geometry(page):
    text, characters = [], []
    states = _text_paint_states(page)
    flags = fitz.TEXTFLAGS_RAWDICT & ~fitz.TEXT_PRESERVE_IMAGES
    for block in page.get_text('rawdict', flags=flags)['blocks']:
        for line in block.get('lines', []):
            for span in line['spans']:
                for char in span['chars']:
                    for code in char['c']:
                        if code.isspace():
                            continue
                        text.append(code)
                        key = (ord(code), round(char['origin'][0], 3), round(char['origin'][1], 3), _font_name_identity(span['font']))
                        characters.append((char['origin'], char['bbox'], span['font'], span['size'], states.get(key, 'unknown')))
                        if len(text) % 4096 == 0:
                            check_cancelled()
                        if len(text) > 2_000_000:
                            raise ValueError('PDF text geometry exceeds two million characters per page')
    return ''.join(text), characters


def _anchor_characters(geometry, token):
    text, characters = geometry
    if not token or text.count(token) != 1:
        return None
    start = text.index(token)
    return characters[start:start + len(token)]


def locate_anchor(geometry, token):
    selected = _anchor_characters(geometry, token)
    if selected is None:
        return None
    bounds = fitz.Rect(selected[0][1])
    for char in selected[1:]:
        bounds |= fitz.Rect(char[1])
    return {'origin_pt': list(selected[0][0]), 'bounds_pt': list(bounds),
            'fonts': [{'name': font, 'size_pt': size} for font, size in
                      sorted({(c[2], round(c[3], 4)) for c in selected})]}


def compare_anchor_geometry(reference, candidate, matched):
    before = {}; after = {}; measured = []; missing = 0
    normalizations = {}
    for anchor in matched:
        check_cancelled()
        first, second = anchor['reference_page'] - 1, anchor['candidate_page'] - 1
        if first not in before:
            before[first] = page_text_geometry(reference[first])
        if second not in after:
            after[second] = page_text_geometry(candidate[second])
        a = locate_anchor(before[first], anchor['anchor'])
        b = locate_anchor(after[second], anchor['anchor'])
        if a is None or b is None:
            missing += 1
            continue
        aa = _anchor_characters(before[first], anchor['anchor'])
        bb = _anchor_characters(after[second], anchor['anchor'])
        state = lambda char: char[4] if len(char) > 4 else 'unknown'
        painting = [(x, y) for x, y in zip(aa, bb) if state(x) == state(y) == 'paint_requested']
        nonpainting = [(x, y) for x, y in zip(aa, bb) if 'nonpainting' in (state(x), state(y))]
        sizes = [abs(x[3] - y[3]) for x, y in zip(aa, bb)]
        character_deltas = [[y[0][n] - x[0][n] for n in (0, 1)] for x, y in zip(aa, bb)]
        for char in aa + bb:
            identity = _font_name_identity(char[2])
            if identity != char[2]:
                normalizations[char[2]] = identity
        measured.append({'paragraph': anchor['paragraph'], 'reference_page': first + 1,
            'candidate_page': second + 1, 'reference': a, 'candidate': b,
            'origin_delta_pt': [b['origin_pt'][n] - a['origin_pt'][n] for n in (0, 1)],
            'same_font_names': {f['name'] for f in a['fonts']} == {f['name'] for f in b['fonts']},
            'same_font_declarations': a['fonts'] == b['fonts'],
            'matched_nonspace_characters': len(aa),
            'font_name_changed_characters': sum(x[2] != y[2] for x, y in zip(aa, bb)),
            'encoding_normalized_font_name_changed_characters': sum(
                _font_name_identity(x[2]) != _font_name_identity(y[2]) for x, y in zip(aa, bb)),
            'positive_opacity_paired_characters': len(painting),
            'positive_opacity_font_name_changed_characters': sum(_font_name_identity(x[2]) != _font_name_identity(y[2]) for x, y in painting),
            'nonpainting_paired_characters': len(nonpainting),
            'nonpainting_font_name_changed_characters': sum(_font_name_identity(x[2]) != _font_name_identity(y[2]) for x, y in nonpainting),
            'unknown_paint_state_paired_characters': len(aa) - len(painting) - len(nonpainting),
            'character_origin_delta_pt': character_deltas,
            'median_absolute_font_size_delta_pt': median(sizes),
            'max_absolute_font_size_delta_pt': max(sizes)})
    signed = [median(a['origin_delta_pt'][n] for a in measured) if measured else None for n in (0, 1)]
    for anchor in measured:
        anchor['residual_from_median_origin_pt'] = [anchor['origin_delta_pt'][n] - signed[n] for n in (0, 1)]
        anchor['max_absolute_character_origin_residual_pt'] = [
            max(abs(delta[n] - signed[n]) for delta in anchor['character_origin_delta_pt']) for n in (0, 1)]
        anchor['max_absolute_character_origin_delta_pt'] = [
            max(abs(delta[n]) for delta in anchor['character_origin_delta_pt']) for n in (0, 1)]
        # Bound report size: per-anchor extrema retain internal glyph drift;
        # do not persist thousands of duplicate character coordinate pairs.
        del anchor['character_origin_delta_pt']
    return {'text_paint_measurement': 'Legacy font counts include all semantic text. Positive-opacity pairs request paint and are not ignore-text; this does not prove unclipped/unoccluded visibility. Nonpainting pairs contain explicit zero-alpha/ignore text. Attribution beyond 200000 trace characters per page remains unknown.',
            **{key: sum(a[key] for a in measured) for key in ('positive_opacity_paired_characters', 'positive_opacity_font_name_changed_characters', 'nonpainting_paired_characters', 'nonpainting_font_name_changed_characters', 'unknown_paint_state_paired_characters')},
            'measurement': 'Raw page points; no alignment, scaling or font-alias equivalence assumptions. '
                'Coordinates concern matched text only, not pictures, tables or full visual fidelity. '
                'Font sizes are native PDF measurements, not original authoring properties. '
                'Median-origin residuals are diagnostic only; they do not transform output or imply a visual match. '
                'Encoding-normalized names only decode reversible Korean bytes/remove subset prefixes; '
                'they do not prove identical font files, outlines or visual fidelity.',
            'anchors': measured, 'missing_geometry': missing,
            'median_absolute_origin_delta_pt': [median(abs(a['origin_delta_pt'][n]) for a in measured)
                if measured else None for n in (0, 1)],
            'median_signed_origin_delta_pt': signed,
            'median_absolute_origin_residual_pt': [median(abs(a['residual_from_median_origin_pt'][n]) for a in measured)
                if measured else None for n in (0, 1)],
            'max_absolute_origin_residual_pt': [max(abs(a['residual_from_median_origin_pt'][n]) for a in measured)
                if measured else None for n in (0, 1)],
            'matched_nonspace_characters': sum(a['matched_nonspace_characters'] for a in measured),
            'font_name_changed_characters': sum(a['font_name_changed_characters'] for a in measured),
            'encoding_normalized_font_name_changed_characters': sum(
                a['encoding_normalized_font_name_changed_characters'] for a in measured),
            'font_name_normalizations': normalizations,
            'max_absolute_character_origin_delta_pt': [max(
                a['max_absolute_character_origin_delta_pt'][n] for a in measured) if measured else None for n in (0, 1)],
            'max_absolute_character_origin_residual_pt': [max(
                a['max_absolute_character_origin_residual_pt'][n] for a in measured) if measured else None for n in (0, 1)],
            'different_font_declarations': sum(not a['same_font_declarations'] for a in measured)}
