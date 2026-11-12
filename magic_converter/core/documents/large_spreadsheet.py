"""Bounded, content-oriented XLSX PDF output without an in-memory cell model."""
from __future__ import annotations

import re
import shutil
import tempfile
import time
import zipfile
from pathlib import Path

from magic_converter.core.jobs.job_control import check_cancelled, report_stage

LARGE_BYTES = 16 * 1024 * 1024
LARGE_ROWS = 100_000
ROWS_PER_BATCH = 256
COLUMNS_PER_PANEL = 6
# Each append parses the growing destination page tree. A bounded larger
# chunk reduces those appends while keeping the same incremental save path.
PAGES_PER_CHUNK = 512
MAX_PAGES = 100_000


def large_workbook(source: Path) -> bool:
    if source.suffix.lower() != '.xlsx' or not source.is_file():
        return False
    if source.stat().st_size >= LARGE_BYTES:
        return True
    with zipfile.ZipFile(source) as archive:
        for name in archive.namelist():
            if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', name):
                check_cancelled()
                with archive.open(name) as stream:
                    prefix = stream.read(65536)
                dimension = re.search(rb'<(?:\w+:)?dimension\b[^>]*\bref=["\']([^"\']+)', prefix)
                if dimension:
                    last = re.search(rb'(\d+)$', dimension[1])
                    if last and int(last[1]) >= LARGE_ROWS:
                        return True
    return False


def stream_workbook_pdf(source: Path, destination: Path) -> dict:
    """Write all populated cells, with row/column identities and explicit loss.

    This is a data fallback, not Excel's print-layout renderer. Formulas remain
    formula text; no macros, external links or recalculation are executed.
    The caller owns atomic publication and its finite worker memory/deadline.
    """
    from magic_converter.core.jobs.phase_trace import enabled, mark, perf_counter_ns
    trace = enabled()
    mark("workbook_start")
    import pymupdf as fitz
    from openpyxl import load_workbook
    from openpyxl.utils import get_column_letter
    from magic_converter.core.documents.spreadsheet_formula import describe_special_formula
    from magic_converter.core.documents.workbook_text import needs_shaping, write_shaped_page, write_plain_page
    from magic_converter.core.documents.workbook_layout import column_count

    book = writer = parts_directory = None
    chunk_pages=merged_pages=chunk_number=0
    chunk_path=None
    result = {'sheets': 0, 'rows': 0, 'cells': 0, 'formulas': 0, 'pages': 0}
    media = fitz.Rect(0, 0, 842, 595)
    font=fitz.Font('cjk');font_size=7;line_height=9
    advances={}
    timing = dict(open_ns=0, read_and_count_ns=0, plan_ns=0, render_ns=0, save_ns=0, batch_ns=0)

    def finish_chunk():
        nonlocal writer,chunk_pages,merged_pages
        if writer is None:return
        started = perf_counter_ns() if trace else 0
        if chunk_pages:
            compact=chunk_path
            writer.subset_fonts()
            writer.save(compact,garbage=4,deflate=True)
            writer.close();writer=None
            if merged_pages:
                with fitz.open(destination) as combined:
                    with fitz.open(compact) as chunk:
                        combined.insert_pdf(chunk, links=False, annots=False)
                    combined.saveIncr()
            else:shutil.copyfile(compact,destination)
            merged_pages+=chunk_pages
        else:
            writer.close();writer=None
        chunk_path.unlink(missing_ok=True)
        chunk_pages=0
        if trace:
            timing['save_ns'] += perf_counter_ns() - started

    def next_page():
        nonlocal writer,chunk_path,chunk_number
        check_cancelled()
        if chunk_pages>=PAGES_PER_CHUNK:finish_chunk()
        if writer is None:
            chunk_number+=1
            chunk_path=Path(parts_directory.name)/f'part-{chunk_number:06}.pdf'
            writer=fitz.open()
        result['pages'] += 1
        if result['pages'] > MAX_PAGES:
            raise ValueError('대형 표 PDF의 100000쪽 한도를 넘었습니다.')

    def batch(title, rows):
        nonlocal chunk_pages
        started = perf_counter_ns() if trace else 0
        # A long value must not expand thirteen mostly numeric cells into a
        # very tall table row. Adapt between three and six address-labelled
        # lanes, retaining the same font size and wider lanes on a page tie.
        # Font advances determine wrapping; no hard character truncation.
        page=None;lane=0;y=0;page_lines=[];complex_page=False;header=''
        columns=column_count(rows,media.width-48,COLUMNS_PER_PANEL,font,font_size,advances)
        if trace:
            timing['plan_ns'] += perf_counter_ns() - started
        lane_width=(media.width-48)/columns
        def flush_page():
            nonlocal page,chunk_pages,page_lines,complex_page
            if page is None:return
            render_started = perf_counter_ns() if trace else 0
            if complex_page:
                write_shaped_page(page,header,page_lines,Path(parts_directory.name),font,columns)
            else:
                write_plain_page(page,header,page_lines,font,columns)
            if trace:
                timing['render_ns'] += perf_counter_ns() - render_started
            chunk_pages+=1
            page=None
            page_lines=[];complex_page=False
        def line(value):
            nonlocal page,lane,y,complex_page,header
            if page is not None and y>568:
                lane+=1;y=46
                if lane>=columns:flush_page()
            if page is None:
                next_page();page=writer.new_page(width=media.width,height=media.height)
                header=f'{title} / {rows[0][0]}-{rows[-1][0]} / Cell values and formulas'
                complex_page=needs_shaping(header)
                lane=0;y=46
            page_lines.append(value)
            complex_page|=needs_shaping(value)
            y+=line_height
        for index,values in rows:
            for column,value in enumerate(values,1):
                if value is None:continue
                check_cancelled()
                address=f'{get_column_letter(column)}{index}';continuation=0
                for paragraph in str(value).split('\n'):
                    remaining=paragraph
                    while True:
                        prefix=address+(f'+{continuation}'if continuation else'')+' '
                        for character in prefix:
                            if character not in advances:
                                advances[character]=font.glyph_advance(ord(character))*font_size
                        available=lane_width-10-sum(advances[character]for character in prefix)
                        count=0;used=0
                        for character in remaining:
                            if character not in advances:
                                advances[character]=font.glyph_advance(ord(character))*font_size
                            width=advances[character]
                            if used+width>available:break
                            used+=width;count+=1
                        if remaining and not count:raise ValueError('셀 문자를 PDF 열 너비에 배치할 수 없습니다.')
                        line(prefix+remaining[:count]);continuation+=1
                        remaining=remaining[count:]
                        if not remaining:break
        if page is None and not any(value is not None for _,values in rows for value in values):line('Empty worksheet')
        flush_page()
        if trace:
            timing['batch_ns'] += perf_counter_ns() - started

    try:
        opened = perf_counter_ns() if trace else 0
        book = load_workbook(source, read_only=True, data_only=False, keep_links=False)
        parts_directory=tempfile.TemporaryDirectory(prefix='.magic-xlsx-parts-',dir=destination.parent)
        reading = perf_counter_ns() if trace else 0
        if trace:
            timing['open_ns'] = reading - opened
        last_update = 0
        for sheet in book:
            check_cancelled()
            result['sheets'] += 1
            # A declared A1 dimension must not silently hide later populated
            # rows/columns in a malformed or producer-generated worksheet.
            sheet.reset_dimensions()
            rows = [];batch_cells=0
            for number, cells in enumerate(sheet.iter_rows(), 1):
                check_cancelled()
                values = []
                populated = formulas = 0
                for cell in cells:
                    value = cell.value
                    if value is not None:
                        populated += 1
                        if cell.data_type == "f":
                            formulas += 1
                            if not isinstance(value, str):
                                value = describe_special_formula(value)
                    values.append(value)
                if not populated:
                    continue
                result['rows'] += 1
                result['cells'] += populated
                result['formulas'] += formulas
                rows.append((number, values))
                batch_cells+=len(values)
                if len(rows) >= ROWS_PER_BATCH or batch_cells>=4096:
                    batch(sheet.title, rows)
                    rows = [];batch_cells=0
                if time.monotonic() - last_update >= 2:
                    report_stage(f'대형 표 전체 내용 출력 중: {result["rows"]}행')
                    last_update = time.monotonic()
            if rows:
                batch(sheet.title, rows)
        if trace:
            timing['read_and_count_ns'] = perf_counter_ns() - reading - timing['batch_ns']
        if not result['pages']:
            batch('Empty workbook', [(1, ())])
        saved_in_batches = timing['save_ns']
        finish_chunk()
        with fitz.open(destination)as pdf:
            if len(pdf)!=result['pages']:raise ValueError('대형 표 PDF의 쪽수가 완료 기록과 다릅니다.')
        check_cancelled()
        if trace:
            timing['wrap_and_page_ns'] = timing.pop('batch_ns') - timing['plan_ns'] - timing['render_ns'] - saved_in_batches
            mark("workbook_done", **timing, **result, chunks=chunk_number)
        return result
    except BaseException as error:
        # Drop completed layout frames before removing private files on Windows.
        import traceback
        traceback.clear_frames(error.__traceback__)
        # Failed native PDF streams must never look like complete output to
        # a direct caller, even before the supervisor removes its private scope.
        if writer is not None:
            try:
                writer.close()
            except Exception:
                pass
            writer = None
        import gc
        gc.collect()
        destination.unlink(missing_ok=True)
        raise
    finally:
        if writer is not None:
            writer.close()
        if book is not None:
            book.close()
        if parts_directory is not None:
            parts_directory.cleanup()
