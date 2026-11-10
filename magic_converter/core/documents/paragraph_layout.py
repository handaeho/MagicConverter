"""Point-valued paragraph geometry; never infer a printer origin correction."""
import math

FIELDS = ('left_indent', 'right_indent', 'first_line_indent', 'space_before', 'space_after')
HWPX_FIELDS = dict(zip(FIELDS, ('left', 'right', 'intent', 'prev', 'next')))


def hwpx_layout(prop, warnings):
    from magic_converter.core.documents.hwp_documents import q
    margin = prop.find(q('hh', 'margin'))
    if margin is None:
        # Prefer the supported fallback over HwpUnitChar alternatives.
        margin = prop.find('.//' + q('hp', 'default') + '/' + q('hh', 'margin'))
    result = {}
    if margin is None:
        return result
    for name, tag in HWPX_FIELDS.items():
        node = margin.find(q('hc', tag))
        if node is None:
            continue
        try:
            if node.get('unit') != 'HWPUNIT':
                raise ValueError('Unsupported paragraph unit')
            value = int(node.get('value')) / 100
            if abs(value) > 1_000_000 or name.startswith('space_') and value < 0:
                raise ValueError('Invalid paragraph geometry')
            result[name] = value
        except (TypeError, ValueError, OverflowError):
            warning = '일부 HWPX 문단 여백·간격 단위를 보존할 수 없어 기본값을 사용합니다.'
            if warning not in warnings:
                warnings.append(warning)
    return result


def word_layout(paragraph):
    result = {}
    for name in FIELDS:
        value = getattr(paragraph.paragraph_format, name)
        style, seen = paragraph.style, set()
        for _ in range(64):
            if value is not None or style is None or style.style_id in seen:
                break
            seen.add(style.style_id)
            value = getattr(style.paragraph_format, name)
            style = style.base_style
        if value is not None:
            points = value.pt
            if math.isfinite(points):
                result[name] = points
    return result
