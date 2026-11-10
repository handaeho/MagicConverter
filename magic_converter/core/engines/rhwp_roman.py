from copy import deepcopy
import re
import struct

from lxml import etree
from defusedxml import ElementTree as SafeET

from magic_converter.core.jobs.job_control import check_cancelled

H = 'http://www.hancom.co.kr/hwpml/2011/head'
P = 'http://www.hancom.co.kr/hwpml/2011/paragraph'
SLOTS = ('hangul', 'latin', 'hanja', 'japanese', 'other', 'symbol', 'user')
ROMANS = '\u216a\u216b'


def available_replacements(directories):
    from magic_converter.core.pdf.pdf_fonts import resolve_font, font_metrics, _faces, font_key
    result = {}
    pass
    return result


def normalize(header_data, sections, replacements):
    """Return changed XML only for plain text runs with unambiguous font slots."""
    if not replacements:
        return header_data, sections, []
    SafeET.fromstring(header_data)
    parser = etree.XMLParser(resolve_entities=False, no_network=True)
    header = etree.fromstring(header_data, parser)
    fonts = {node.get('lang').lower(): node for node in header.iter(f'{{{H}}}fontface') if node.get('lang')}
    properties = next(header.iter(f'{{{H}}}charProperties'), None)
    if properties is None or set(fonts) != set(SLOTS):
        return header_data, sections, []
    styles = {node.get('id'): node for node in properties}
    new_styles, changed, output = {}, [], dict(sections)
    split_budget = 4096

    def replacement_style(style_id):
        if style_id in new_styles:
            return new_styles[style_id]
        style = styles.get(style_id)
        ref = style.find(f'{{{H}}}fontRef') if style is not None else None
        if ref is None or len(new_styles) >= 256:
            return None
        entries = {}
        for slot in SLOTS:
            matches = [f for f in fonts[slot] if f.get('id') == ref.get(slot)]
            if len(matches) != 1 or matches[0].get('isEmbedded') not in (None, '0'):
                return None
            entries[slot] = matches[0]
        names = {node.get('face') for node in entries.values()}
        if len(names) != 1 or next(iter(names)) not in replacements:
            return None
        family, missing = replacements[next(iter(names))]
        copied = deepcopy(style)
        copied.set('id', str(max(int(n) for n in styles) + len(new_styles) + 1))
        for slot in SLOTS:
            collection = fonts[slot]
            existing = next((f for f in collection if f.get('face') == family and f.get('isEmbedded') in (None, '0')), None)
            if existing is None:
                existing = deepcopy(entries[slot])
                existing.set('id', str(max(int(f.get('id')) for f in collection) + 1))
                existing.set('face', family); existing.set('type', 'TTF'); existing.set('isEmbedded', '0')
                for child in list(existing):
                    if child.tag.endswith('}substFont'):
                        existing.remove(child)
                collection.append(existing); collection.set('fontCnt', str(len(collection)))
            copied.find(f'{{{H}}}fontRef').set(slot, existing.get('id'))
        properties.append(copied); properties.set('itemCnt', str(len(properties)))
        new_styles[style_id] = (copied.get('id'), missing, family)
        return new_styles[style_id]

    for name, data in sections.items():
        SafeET.fromstring(data)
        section = etree.fromstring(data, parser)
        edits = 0
        for run in list(section.iter(f'{{{P}}}run')):
            check_cancelled()
            if len(changed) >= 4096:
                break
            if len(run) != 1 or run[0].tag != f'{{{P}}}t' or len(run[0]):
                continue  # Never split fields, pictures, controls or inline markup.
            text = run[0].text or ''
            if not any(c in text for c in ROMANS):
                continue
            selected = replacement_style(run.get('charPrIDRef'))
            if selected is None:
                continue
            style_id, missing, family = selected
            if not any(c in text for c in missing):
                continue
            pieces, end = [], 0
            for match in re.finditer('[' + ''.join(sorted(missing)) + ']+', text):
                check_cancelled()
                if match.start() > end:
                    pieces.append(text[end:match.start()])
                pieces.append(match.group()); end = match.end()
                if len(pieces) > split_budget:
                    break
            if end < len(text):
                pieces.append(text[end:])
            if len(pieces) > split_budget:
                continue  # No partial rewriting when the split budget is spent.
            split_budget -= len(pieces)
            parent = run.getparent(); position = parent.index(run)
            for index, part in enumerate(pieces):
                node = deepcopy(run); node[0].text = part
                if part[0] in missing:
                    node.set('charPrIDRef', style_id)
                node.tail = run.tail if index == len(pieces) - 1 else None
                parent.insert(position + index, node)
            parent.remove(run); edits += 1
            changed.append({'section': name, 'operation': 'roman_missing_glyph', 'replacement_family': family,
                            'characters': ''.join(c for c in text if c in missing)})
        if edits:
            output[name] = etree.tostring(section, encoding='UTF-8', xml_declaration=True)
    if not changed:
        return header_data, sections, []
    return etree.tostring(header, encoding='UTF-8', xml_declaration=True), output, changed
