"""Shaped workbook pages with standard PDF replacement text in logical order."""
from __future__ import annotations

import html
import unicodedata
from functools import lru_cache
from pathlib import Path

from magic_converter.core.jobs.job_control import check_cancelled


@lru_cache(maxsize=4096)
def _complex_character(character: str) -> bool:
    return (unicodedata.bidirectional(character) in {'R', 'AL', 'AN'}
            or unicodedata.category(character) in {'Mn', 'Mc', 'Me', 'Cf'})


def needs_shaping(text: str) -> bool:
    return any(ord(character) > 127 and _complex_character(character) for character in text)


def write_plain_page(page, header: str, lines: list[str], font, columns: int) -> None:
    """Write fixed baselines without recalculating all prior glyph bounds.

    The pinned TextWriter.append calls this same MuPDF primitive, then scans
    the accumulated text bounds. This writer never consumes those bounds.
    Font fallback, glyph advances, encoding and the PDF text device are shared.
    """
    import pymupdf as fitz
    from magic_converter.core.documents.workbook_layout import LINES_PER_COLUMN

    text_writer = fitz.TextWriter(page.rect)
    m = fitz.mupdf
    language = m.fz_text_language_from_string(None)
    lane_width = (page.rect.width - 48) / columns
    height = page.rect.height
    for number, value in enumerate([header, *lines]):
        check_cancelled()
        if number == 0:
            x, y, size = 24, 30, 8
        else:
            lane, offset = divmod(number - 1, LINES_PER_COLUMN)
            x, y, size = 24 + lane * lane_width, 46 + offset * 9, 7
        matrix = m.fz_make_matrix(size, 0, 0, size, x, height - y)
        m.fz_show_string(text_writer.this, font.this, matrix, value, 0, 0, 0, language)
    if font.flags['mono']:
        text_writer.used_fonts.add(font)
    text_writer.write_text(page)


def write_shaped_page(page, header: str, lines: list[str], folder: Path, font, columns: int = 3) -> None:
    """Keep visible shaping and original Unicode together, without a hidden layer.

    MuPDF's simple text writer and some extractors expose visual-order Arabic
    presentation forms. Story shapes the visible glyphs; one enclosing PDF
    ActualText span supplies the page's logical text to supporting readers.
    The replacement span covers the entire visible page content.
    """
    import pymupdf as fitz

    font_path = folder / 'workbook-font.ttf'
    if not font_path.exists():
        font_path.write_bytes(font.buffer)
    source = ('<div class="heading">' + html.escape(header) + '</div><pre>'
              + html.escape('\n'.join(lines)) + '</pre>')
    css = '''
        @font-face { font-family: workbook; src: url(workbook-font.ttf); }
        body, div, pre { margin: 0; padding: 0; }
        pre { font-family: workbook; font-size: 7pt; line-height: 9pt; }
        .heading { font-family: workbook; font-size: 8pt; line-height: 12pt; }
    '''
    story = fitz.Story(source, user_css=css, archive=fitz.Archive(str(folder)))
    lane_width = (page.rect.width - 48) / columns
    temporary = folder / 'shaped-page.pdf'
    output = fitz.DocumentWriter(str(temporary))
    try:
        device = output.begin_page(page.rect)
        more, _ = story.place(fitz.Rect(24, 18, 818, 35))
        story.draw(device)
        for lane in range(columns):
            check_cancelled()
            if not more:
                break
            more, _ = story.place(fitz.Rect(24 + lane * lane_width, 38,
                                            24 + (lane + 1) * lane_width, 584))
            story.draw(device)
        if more:
            raise ValueError('복합 문자 셀을 PDF 페이지 안에 모두 배치할 수 없습니다.')
        output.end_page()
    finally:
        output.close()
        output = device = None
    try:
        # Grafting can retain the donor document internally. An in-memory
        # donor avoids keeping a private Windows file handle alive afterward.
        with fitz.open(stream=temporary.read_bytes(), filetype='pdf') as shaped:
            if len(shaped) != 1:
                raise ValueError('복합 문자 페이지 수가 올바르지 않습니다.')
            for block in shaped[0].get_text('blocks'):
                if not page.rect.contains(fitz.Rect(block[:4])):
                    raise ValueError('복합 문자 셀이 PDF 페이지 경계를 넘었습니다.')
            # Extractors may group adjacent columns into one line. Check
            # actual glyph boxes, which do not depend on that grouping.
            for block in shaped[0].get_text('rawdict')['blocks']:
                for line in block.get('lines', []):
                    for span in line['spans']:
                        for character in span['chars']:
                            bounds = fitz.Rect(character['bbox'])
                            if bounds.y0 >= 35 and not any(
                                    fitz.Rect(23.9 + lane * lane_width, 35,
                                              24.1 + (lane + 1) * lane_width, 595).contains(bounds)
                                    for lane in range(columns)):
                                raise ValueError('복합 문자 셀이 PDF 열 경계를 넘었습니다.')
            page.show_pdf_page(page.rect, shaped, 0)
        page.clean_contents()
        contents = page.get_contents()
        if len(contents) != 1:
            raise ValueError('복합 문자 페이지의 내용 스트림을 확인할 수 없습니다.')
        logical = '\n'.join([header, *lines])
        replacement = ('\ufeff' + logical).encode('utf-16-be').hex().encode('ascii')
        xref = contents[0]
        page.parent.update_stream(
            xref, b'/Span << /ActualText <' + replacement
            + b'> /MagicWorkbookText true >> BDC\n'
            + page.parent.xref_stream(xref) + b'\nEMC\n')
    finally:
        temporary.unlink(missing_ok=True)
