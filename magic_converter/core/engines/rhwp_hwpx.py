from pathlib import Path
import re
import zipfile

from defusedxml import ElementTree as ET
from lxml import etree

from magic_converter.core.jobs.job_control import check_cancelled, copy_stream

NS = {'p': 'http://www.hancom.co.kr/hwpml/2011/paragraph'}
MAX_XML = 16 * 1024 ** 2
MAX_ARCHIVE = 256 * 1024 ** 2


def tail_height(table, zero_spacing_styles):
    """Return replacement last-row cells/height only for unambiguous slack."""
    pos, size = table.find('p:pos', NS), table.find('p:sz', NS)
    if (pos is None or size is None or pos.get('treatAsChar') != '0'
            or table.get('textWrap') not in {'SQUARE', 'TOP_AND_BOTTOM'}
            or table.get('noAdjust') != '0' or table.get('cellSpacing') != '0'):
        return None
    try:
        rows = table.findall('p:tr', NS)
        cols = int(table.get('colCnt'))
        if len(rows) < 2 or len(rows) != int(table.get('rowCnt')) or cols < 1:
            return None
        heights = []
        for row_index, row in enumerate(rows):
            cells = row.findall('p:tc', NS)
            if len(cells) != cols:
                return None
            values = []
            for col, cell in enumerate(cells):
                span, addr, cell_size = (cell.find('p:' + name, NS) for name in ('cellSpan', 'cellAddr', 'cellSz'))
                if (span is None or addr is None or cell_size is None
                        or span.get('rowSpan') != '1' or span.get('colSpan') != '1'
                        or int(addr.get('rowAddr')) != row_index or int(addr.get('colAddr')) != col):
                    return None
                values.append(int(cell_size.get('height')))
            if len(set(values)) != 1 or values[0] <= 0:
                return None
            heights.append(values[0])
        excess = sum(heights) - int(size.get('height'))
        # Large conflicts can be stale metadata or authored cell constraints.
        # This compatibility case only recovers a small, declared tail slack.
        if not 0 < excess <= sum(heights) * .05:
            return None
        height = heights[-1] - excess
        cells = rows[-1].findall('p:tc', NS)
        for cell in cells:
            paragraphs = cell.findall('p:subList/p:p', NS)
            if len(paragraphs) != 1:
                return None
            paragraph = paragraphs[0]
            if paragraph.get('paraPrIDRef') not in zero_spacing_styles:
                return None
            if any(n.tag not in {f'{{{NS["p"]}}}t'} for run in paragraph.findall('p:run', NS) for n in run):
                return None  # Includes nested tables, pictures, fields and breaks.
            lines = paragraph.findall('p:linesegarray/p:lineseg', NS)
            if not lines or any(int(n.get('vertpos')) < 0 for n in lines):
                return None
            # Include trailing line spacing as well as actual stored ink.
            content = max(int(n.get('vertpos')) + max(int(n.get('vertsize')), int(n.get('textheight')))
                          + max(0, int(n.get('spacing'))) for n in lines)
            margin = cell.find('p:cellMargin', NS) if cell.get('hasMargin') == '1' else table.find('p:inMargin', NS)
            if margin is None:
                return None
            floor = content + int(margin.get('top')) + int(margin.get('bottom'))
            if height < floor:
                return None
        return cells, height, excess
    except (TypeError, ValueError, OverflowError):
        return None


def zero_spacing_styles(data):
    header = ET.fromstring(data)
    result = set()
    for style in header.iter():
        if not style.tag.endswith('}paraPr'):
            continue
        margins = [n for n in style.iter() if n.tag.endswith(('}prev', '}next'))]
        if margins and all(n.get('unit') == 'HWPUNIT' and n.get('value') == '0' for n in margins):
            result.add(style.get('id'))
    return result


def stored_tail_breaks(root):
    """Recover a saved page boundary after hidden floating-table tail blanks.

    The pinned renderer discards these blank hosts after splitting a table,
    then fails to see the following inline table's vpos reset. Require a
    single-column page, a monotonic bottom-of-page blank tail and a reset
    near the top. Mid-page rewinds and text/field hosts are not page breaks.
    """
    try:
        pages = root.findall('.//p:pagePr', NS)
        columns = root.findall('.//p:colPr', NS)
        if len(pages) != 1 or len(columns) != 1 or columns[0].get('colCount') != '1':
            return []
        margin = pages[0].find('p:margin', NS)
        body = int(pages[0].get('height')) - sum(int(margin.get(k)) for k in ('top', 'bottom', 'header', 'footer'))
        if body <= 0:
            return []
        paragraphs = root.findall('p:p', NS)
        blanks, anchor, result = [], None, []
        for index, paragraph in enumerate(paragraphs):
            if index % 256 == 0:
                check_cancelled()
            lines = paragraph.findall('p:linesegarray/p:lineseg', NS)
            runs = paragraph.findall('p:run', NS)
            empty = all(n.tag == f'{{{NS["p"]}}}t' and not (n.text or '') for run in runs for n in run)
            plain = all(n.tag in {f'{{{NS["p"]}}}run', f'{{{NS["p"]}}}linesegarray'} for n in paragraph)
            no_break = paragraph.get('pageBreak') == '0' and paragraph.get('columnBreak') == '0'
            if empty and plain and no_break and len(lines) == 1:
                line = lines[0]
                y, height = int(line.get('vertpos')), int(line.get('vertsize'))
                if height > 0 and 0 <= y < y + height <= body:
                    blanks.append((y, height))
                    continue
            tables = paragraph.findall('p:run/p:tbl', NS)
            if (anchor is not None and len(blanks) >= 2 and len(lines) == 1 and no_break
                    and len(tables) == 1 and not any((t.text or '').strip() for t in paragraph.findall('p:run/p:t', NS))):
                table = tables[0]
                pos = table.find('p:pos', NS)
                if (pos is not None and pos.get('treatAsChar') == '1' and table.get('rowCnt') == '1'
                        and table.get('textWrap') == 'TOP_AND_BOTTOM'
                        and 0 <= int(lines[0].get('vertpos')) <= body * .05
                        and blanks[0][0] >= body * .75 and blanks[-1][0] >= body * .9
                        and all(a[0] + a[1] <= b[0] for a, b in zip(blanks, blanks[1:]))):
                    result.append(index)
            blanks, anchor = [], None
            if len(tables) == 1 and no_break:
                table = tables[0]
                pos, size = table.find('p:pos', NS), table.find('p:sz', NS)
                if (pos is not None and size is not None and pos.get('treatAsChar') == '0'
                        and table.get('pageBreak') in {'CELL', 'TABLE'} and int(table.get('rowCnt')) >= 2
                        and body * .5 <= int(size.get('height')) < body):
                    anchor = index
        return result
    except (AttributeError, TypeError, ValueError, OverflowError):
        return []


def normalize_section(data, styles):
    # Validate entity/DTD safety before using the prefix-preserving serializer.
    ET.fromstring(data)
    root = etree.fromstring(data, etree.XMLParser(resolve_entities=False, no_network=True))
    changed = []
    for index in stored_tail_breaks(root):
        root.findall('p:p', NS)[index].set('pageBreak', '1')
        changed.append({'paragraph': index, 'operation': 'stored_tail_page_break'})
    for paragraph_index, paragraph in enumerate(root.findall('p:p', NS)):
        check_cancelled()
        # Only empty table hosts: adjacent authored text has separate height
        # requirements that this compatibility operation cannot establish.
        if any((t.text or '').strip() for t in paragraph.findall('p:run/p:t', NS)):
            continue
        for table in paragraph.findall('p:run/p:tbl', NS):
            result = tail_height(table, styles)
            if result is None:
                continue
            cells, height, excess = result
            for cell in cells:
                cell.find('p:cellSz', NS).set('height', str(height))
            changed.append({'paragraph': paragraph_index, 'operation': 'table_tail_height', 'table_id': table.get('id'),
                            'recovered_hwp_units': excess, 'tail_height_hwp_units': height})
    return (etree.tostring(root, encoding='UTF-8', xml_declaration=True) if changed else data), changed


def prepare(source: Path, folder: Path):
    """Return original or a private compatibility archive plus diagnostics."""
    if source.suffix.lower() != '.hwpx' or not zipfile.is_zipfile(source):
        return source, []
    changed, sections = [], {}
    with zipfile.ZipFile(source) as archive:
        entries = archive.infolist()
        # Leave large/ambiguous archives unchanged; never reject valid inputs
        # merely because the optional compatibility pass cannot inspect them.
        if (len(entries) > 10000 or len({i.filename for i in entries}) != len(entries)
                or sum(i.file_size for i in entries) > MAX_ARCHIVE
                or any(i.flag_bits & 1 for i in entries)):
            return source, []
        if 'Contents/header.xml' not in archive.namelist() or archive.getinfo('Contents/header.xml').file_size > MAX_XML:
            return source, []
        header = archive.read('Contents/header.xml')
        styles = zero_spacing_styles(header)
        for item in entries:
            if re.fullmatch(r'Contents/section\d+\.xml', item.filename):
                if item.file_size > MAX_XML:
                    return source, []
                data, edits = normalize_section(archive.read(item), styles)
                sections[item.filename] = data
                if edits:
                    changed.extend(dict(section=item.filename, **edit) for edit in edits)
        from magic_converter.core.engines.rhwp_roman import available_replacements, normalize
        from magic_converter.core.engines.rhwp_engine import font_directories
        try:
            candidate_header, candidate_sections, edits = normalize(header, sections, available_replacements(font_directories()))
            if len(candidate_header) <= MAX_XML and all(len(data) <= MAX_XML for data in candidate_sections.values()):
                header, sections = candidate_header, candidate_sections
                changed.extend(edits)
        except (ValueError, TypeError, OverflowError, KeyError):
            pass  # Optional font compatibility cannot reject a valid archive.
        if not changed:
            return source, []
        destination = folder / 'layout.hwpx'
        with zipfile.ZipFile(destination, 'w') as output:
            for item in entries:
                check_cancelled()
                if item.filename == 'Contents/header.xml':
                    output.writestr(item, header)
                elif item.filename in sections:
                    output.writestr(item, sections[item.filename])
                else:
                    with archive.open(item) as incoming, output.open(item, 'w') as outgoing:
                        copy_stream(incoming, outgoing)
    return destination, changed
