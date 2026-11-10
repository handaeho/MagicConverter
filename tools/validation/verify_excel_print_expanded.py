"""Verify expanded native XLSX print support against two Excel printer queues."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter.core.engines.office_com import EXCEL_VALIDATION_PRINTER
from tools.validation.verify_excel_print import (ACTION, ProcessLedger, application, cleanup_owned_processes,
                                                compare, run_helper, save_json, setup, sha)


QUEUES = ('samsung', 'microsoft-pdf')
LEGACY = ('controls', 'multi-area', 'titles', 'too-many-areas')
POSITIVE = (*LEGACY[:3], 'auto-values', 'titles-outside-single', 'titles-outside-multi', 'chart-mixed',
            'limit-10000', 'limit-10001', 'limit-20000', 'large-20000', 'large-titles-multi', 'pagination')
NEGATIVE = {'too-many-areas': '직사각형 인쇄 영역', 'auto-empty': '값이나 수식', 'auto-format-only': '값이나 수식',
            'auto-style-tail': '자동 인쇄 영역', 'auto-sparse': '20,000셀', 'two-visible-charts': '차트 시트는 한 개',
            'limit-20001': '20,000셀', 'limit-titles-over': '20,000셀', 'large-20001': '20,000셀'}
AUTO_DIRECT = ('auto-empty', 'auto-format-only', 'auto-style-tail', 'auto-sparse')
CASES = (*POSITIVE, *NEGATIVE)
LARGE = ('large-20000', 'large-20001', 'large-titles-multi')


def read_json(path):
    return json.loads(path.read_text(encoding='utf-8'))


def digest(string):
    return sha256(string.encode('utf-8')).hexdigest()


def save(book, out, name):
    book.SaveAs(str(out / (name + '.xlsx')), FileFormat=51)


def snapshot(book, out, name):
    book.SaveCopyAs(str(out / (name + '.xlsx')))


def grid(sheet, rows, columns):
    sheet.Range(sheet.Cells.Item(1, 1), sheet.Cells.Item(rows, columns)).Value = tuple(
        tuple(f'R{row:03}C{column:02}' for column in range(1, columns + 1)) for row in range(1, rows + 1))


def automatic(excel, out):
    book = excel.Workbooks.Add(-4167)
    try:
        sheet = book.Worksheets.Item(1)
        setup(sheet, '', orientation=2, fit=True)
        save(book, out, 'auto-empty')
        sheet.Range('D5').Interior.Color = 0xD0E5FA
        snapshot(book, out, 'auto-format-only')
        sheet.Range('D5').Clear()
        sheet.Range('B2').Value = 'AUTO START'
        sheet.Range('D5').Formula = '=SUM(19,23)'
        sheet.Range('C4').Formula = '=IF(1=1,"","unused")'
        snapshot(book, out, 'auto-values')
        sheet.Range('J101').Interior.Color = 0xD0E5FA
        snapshot(book, out, 'auto-style-tail')
        sheet.Range('A20001').Value = 'DISTANT CELL'
        snapshot(book, out, 'auto-sparse')
    finally:
        book.Close(False)


def title_cases(excel, out):
    book = excel.Workbooks.Add(-4167)
    try:
        sheet = book.Worksheets.Item(1)
        grid(sheet, 34, 8)
        sheet.Range('A1').Value = 'OUTSIDE TITLE'
        sheet.Range('A1:H34').ColumnWidth = 13
        sheet.Range('A1:H34').RowHeight = 14
        setup(sheet, '$B$3:$H$34', orientation=2, fit=False)
        sheet.PageSetup.PrintTitleRows = '$1:$2'
        sheet.PageSetup.PrintTitleColumns = '$A:$A'
        sheet.HPageBreaks.Add(Before=sheet.Range('A18'))
        sheet.VPageBreaks.Add(Before=sheet.Range('F1'))
        save(book, out, 'titles-outside-single')
        sheet.ResetAllPageBreaks()
        sheet.PageSetup.PrintArea = '$B$3:$H$12,$B$20:$H$29'
        snapshot(book, out, 'titles-outside-multi')
    finally:
        book.Close(False)


def chart_cases(excel, out):
    book = excel.Workbooks.Add(-4167)
    try:
        sheet = book.Worksheets.Item(1)
        sheet.Name = 'Before chart'
        sheet.Range('A1').Value = 'BEFORE CHART SHEET'
        data = sheet.Range('A4:B7')
        data.Value = (('Period', 'Value'), ('First', 15), ('Second', 22), ('Third', 31))
        setup(sheet, '$A$1:$F$10', orientation=2, fit=True)
        chart = book.Charts.Add(After=sheet)
        chart.ChartType = 51
        chart.SetSourceData(data)
        chart.HasTitle = True
        chart.ChartTitle.Text = 'VISIBLE CHART SHEET'
        chart.PageSetup.PaperSize = 9
        chart.PageSetup.Orientation = 1
        after = book.Worksheets.Add(After=chart)
        after.Range('A1').Value = 'AFTER CHART SHEET'
        setup(after, '$A$1:$D$5', orientation=2, fit=True)
        hidden = book.Worksheets.Add(After=after)
        hidden.Range('A1').Value = 'HIDDEN WORKSHEET SENTINEL'
        hidden.Visible = 0
        other = book.Charts.Add(After=after)
        other.ChartType = 51
        other.SetSourceData(data)
        other.HasTitle = True
        other.ChartTitle.Text = 'HIDDEN CHART SENTINEL'
        other.Visible = 0
        save(book, out, 'chart-mixed')
        other.Visible = -1
        snapshot(book, out, 'two-visible-charts')
    finally:
        book.Close(False)


def limits(excel, out):
    book = excel.Workbooks.Add(-4167)
    try:
        first = book.Worksheets.Item(1)
        grid(first, 1000, 10)
        first.Range('A1002').Value = 'ADDITIONAL CELL'
        setup(first, '$A$1:$J$1000', orientation=2, fit=True)
        save(book, out, 'limit-10000')
        first.PageSetup.PrintArea = '$A$1:$J$1000,$A$1002'
        snapshot(book, out, 'limit-10001')
        first.PageSetup.PrintArea = '$A$1:$J$1000'
        second = book.Worksheets.Add(After=first)
        grid(second, 1001, 10)
        setup(second, '$A$2:$J$1001', orientation=2, fit=True)
        snapshot(book, out, 'limit-20000')
        second.PageSetup.PrintArea = '$A$2:$J$1001,$A$1'
        snapshot(book, out, 'limit-20001')
        second.PageSetup.PrintArea = '$A$2:$J$1001'
        second.PageSetup.PrintTitleRows = '$1:$1'
        snapshot(book, out, 'limit-titles-over')
    finally:
        book.Close(False)


def pagination(excel, out):
    book = excel.Workbooks.Add(-4167)
    try:
        sheet = book.Worksheets.Item(1)
        grid(sheet, 118, 15)
        sheet.Range('A1:O118').RowHeight = 14.6
        sheet.Range('A1:O118').ColumnWidth = 9.7
        setup(sheet, '$A$1:$O$118', orientation=1, fit=False)
        sheet.PageSetup.LeftMargin = sheet.PageSetup.RightMargin = 19
        sheet.PageSetup.TopMargin = sheet.PageSetup.BottomMargin = 23
        save(book, out, 'pagination')
    finally:
        book.Close(False)


def large_cases(excel, original, out):
    working = out / 'large-working.xlsx'
    shutil.copyfile(original, working)
    book = excel.Workbooks.Open(str(working), UpdateLinks=0, ReadOnly=False, AddToMru=False)
    try:
        assert book.Worksheets.Count == 1 and not book.ReadOnly
        sheet = book.Worksheets.Item(1)
        setup(sheet, '$A$1:$J$2000', orientation=2, fit=True)
        snapshot(book, out, 'large-20000')
        sheet.PageSetup.PrintArea = '$A$1:$J$2000,$A$2002'
        snapshot(book, out, 'large-20001')
        setup(sheet, '$B$3:$M$40,$B$97:$M$120', orientation=2, fit=True)
        sheet.PageSetup.PrintTitleRows = '$1:$2'
        sheet.PageSetup.PrintTitleColumns = '$A:$A'
        snapshot(book, out, 'large-titles-multi')
    finally:
        book.Close(False)
    working.unlink()


def create_inputs(excel, ledger, original, prior, out):
    prior_receipt = read_json(prior / 'reference.json')
    for name in LEGACY:
        path = prior / (name + '.xlsx')
        assert sha(path) == prior_receipt['workbooks'][name]['sha256']
        shutil.copyfile(path, out / path.name)
    for author in (automatic, title_cases, chart_cases, limits, pagination):
        ledger.mark('office_export', author.__name__)
        author(excel, out)
    ledger.mark('office_export', 'save copies of the 40 MB original')
    large_cases(excel, original, out)


def direct_references(excel, ledger, out, queue, printer, default):
    import win32print
    from pywintypes import com_error
    import pymupdf as fitz
    directory = out / queue
    directory.mkdir()
    cases = {}
    for name in (*POSITIVE, *AUTO_DIRECT):
        ledger.mark('office_export', name)
        workbook = excel.Workbooks.Open(str(out / (name + '.xlsx')), UpdateLinks=0, ReadOnly=True, AddToMru=False)
        try:
            excel.ActivePrinter = printer['request']
            assert digest(str(excel.ActivePrinter)) == printer['sha256']
            assert win32print.GetDefaultPrinter() == default
            if name.startswith('auto-'):
                used = workbook.Worksheets.Item(1).UsedRange
                assert not workbook.Worksheets.Item(1).PageSetup.PrintArea
                cases[name] = {'used_range': str(used.Address)}
            else:
                cases[name] = {}
            pdf = directory / (name + '.pdf')
            try:
                workbook.ExportAsFixedFormat(0, str(pdf), IgnorePrintAreas=False, OpenAfterPublish=False)
            except com_error as error:
                if name not in AUTO_DIRECT:
                    raise
                cases[name].update(status='excel-rejected', excel_error=error.excepinfo[5])
            else:
                with fitz.open(pdf) as doc:
                    cases[name].update(status='printed', sha256=sha(pdf), pages=len(doc))
                if name in ('pagination', 'large-20000', 'large-titles-multi'):
                    sheet = workbook.Worksheets.Item(1)
                    sheet.DisplayPageBreaks = True
                    cases[name]['page_breaks'] = {
                        'before_rows': [sheet.HPageBreaks.Item(i).Location.Row
                                        for i in range(1, sheet.HPageBreaks.Count + 1)],
                        'before_columns': [sheet.VPageBreaks.Item(i).Location.Column
                                           for i in range(1, sheet.VPageBreaks.Count + 1)],
                    }
        finally:
            workbook.Close(False)
    return cases


def helper(original, out, prior, printers, queue):
    import win32print
    default = win32print.GetDefaultPrinter()
    ledger = ProcessLedger(out / (queue + '.processes.json'))
    try:
        with application('excel', ledger) as excel:
            if queue == 'samsung':
                assert digest(str(excel.ActivePrinter)) == printers[queue]['sha256']
                create_inputs(excel, ledger, original, prior, out)
            cases = direct_references(excel, ledger, out, queue, printers[queue], default)
            version = {'version': str(excel.Version), 'build': str(excel.Build)}
        assert win32print.GetDefaultPrinter() == default
        save_json(out / (queue + '.json'), {'excel': version, 'cases': cases, 'printer_sha256': printers[queue]['sha256'],
                                           'windows_default_unchanged': True, 'native_processes': ledger.owned})
    finally:
        ledger.budget.close()


def prepare(original, out, prior, printer_file):
    before = sha(original)
    for queue in QUEUES:
        try:
            code, errors = run_helper([sys.executable, '-B', __file__, '--source', str(original),
                                       '--output', str(out), '--prior', str(prior), '--printers', str(printer_file),
                                       '--helper', queue], out / (queue + '.state.json'), timeout=300)
            if code:
                raise RuntimeError(errors or f'{queue} Excel reference helper failed')
        finally:
            cleanup_owned_processes(out / (queue + '.processes.json'))
    assert sha(original) == before
    reference = {queue: read_json(out / (queue + '.json')) for queue in QUEUES}
    for row in reference.values():
        process, = row['native_processes']
        assert process['budget_phase'] == 'before_process_resume' and process['commit_limit_bytes'] == 2 * 1024**3
    save_json(out / 'reference.json', {'status': 'passed', 'source_sha256': before, 'original_unchanged': True,
                                      'queues': reference, 'workbooks': {name: sha(out / (name + '.xlsx')) for name in CASES}})
    print('Two native Excel reference sets saved', flush=True)


def semantic_check(name, pdf):
    import pymupdf as fitz
    with fitz.open(pdf) as doc:
        page_text = [page.get_text() for page in doc]
        text = '\n'.join(page_text)
        if name == 'auto-values':
            assert 'AUTO START' in text and '42' in text
        if name == 'titles-outside-single':
            assert len(doc) == 4 and all('OUTSIDE TITLE' in page for page in page_text)
            assert sum('R018C01' in page for page in page_text) == 2
        if name == 'titles-outside-multi':
            assert len(doc) == 2 and all('OUTSIDE TITLE' in page for page in page_text)
            assert 'R003C02' in page_text[0] and 'R020C02' in page_text[1] and 'R013C02' not in text
            assert 'R003C01' in page_text[0] and 'R020C01' in page_text[1]
        if name == 'chart-mixed':
            assert len(doc) == 3 and 'HIDDEN' not in text
            # The saved Excel fixture orders the second-created worksheet first.
            assert all(token in page for token, page in zip(
                ('AFTER CHART SHEET', 'VISIBLE CHART SHEET', 'BEFORE CHART SHEET'), page_text))
        if name == 'large-titles-multi':
            assert len(doc) == 2 and all(page.strip() for page in page_text)
        result = {'pages': len(doc), 'page_dimensions_pt': [list(page.rect)[2:] for page in doc],
                  'page_text_sha256': [digest(page) for page in page_text]}
        if name == 'pagination':
            tokens = [re.findall(r'R\d{3}C\d{2}', page) for page in page_text]
            result['grid_page_extents'] = [[cells[0], cells[-1]] for cells in tokens]
        return result


def verify(original, fixtures, out, executable, printers):
    import win32print
    from tools.validation.verify_large_input_rss import measure
    from magic_converter.core.jobs.specialized_worker import WORKER_OUTPUT_BYTES
    reference = read_json(fixtures / 'reference.json')
    default = win32print.GetDefaultPrinter()
    report = {'status': 'running', 'original_sha256': sha(original), 'reference': reference,
              'executable_sha256': sha(executable) if executable else None, 'cases': {}}
    receipt = out / 'report.json'
    save_json(receipt, report)
    try:
        assert reference['original_unchanged'] and report['original_sha256'] == reference['source_sha256']
        prefix = [str(executable)] if executable else [sys.executable, '-B', str(ROOT / 'run_magic_converter.py')]
        for queue in QUEUES:
            expected_queue = reference['queues'][queue]['printer_sha256']
            assert printers[queue]['sha256'] == expected_queue
            cases = report['cases'][queue] = {}
            for name in CASES:
                folder = out / queue / name
                folder.mkdir(parents=True)
                source = fixtures / (name + '.xlsx')
                before = sha(source)
                assert before == reference['workbooks'][name]
                command = [*prefix, 'convert', str(source), '--action', ACTION, '--timeout', '180',
                           '--data-dir', str(out / 'profile'), '--output', str(folder / 'output'), '--json']
                # A frozen CLI extracts nested notice paths before Excel starts.
                # Keep that path independent of the report and case names.
                with TemporaryDirectory(prefix='ep-', dir=ROOT / '.test-artifacts') as temporary:
                    with patch.dict(os.environ, {EXCEL_VALIDATION_PRINTER: printers[queue]['request'],
                                                 'TEMP': temporary, 'TMP': temporary}):
                        conversion = measure(command, folder / 'measure', expected_ok=name in POSITIVE,
                                             deadline=240, office_directory=temporary)
                case = cases[name] = {'status': 'running', 'conversion': conversion, 'input_sha256': before}
                save_json(receipt, report)
                process, = conversion['office_observation']['processes']
                assert process['application'] == 'excel' and process['budget_phase'] == 'before_process_resume'
                assert process['native_commit_limit_bytes'] == 2 * 1024**3 and process['print_queue_sha256'] == expected_queue
                assert not conversion['office_observation']['lingering_before_verifier_cleanup']
                assert not conversion['lingering_owned_pids'] and sha(source) == before
                assert win32print.GetDefaultPrinter() == default
                if name in POSITIVE:
                    output, = conversion['outputs']
                    assert 0 < output['bytes'] <= WORKER_OUTPUT_BYTES and conversion['seconds'] < 180
                    pdf = fixtures / queue / (name + '.pdf')
                    assert sha(pdf) == reference['queues'][queue]['cases'][name]['sha256']
                    target = Path(output['path'])
                    case['dpi'] = {str(dpi): compare(pdf, target, dpi=dpi) for dpi in (96, 144, 200)}
                    assert all(row['status'] == 'matched' and all(page['mae'] == 0 for page in row['pages'])
                               for row in case['dpi'].values()), f'{queue}/{name} differs from Excel'
                    case.update(semantic_check(name, target))
                else:
                    errors = ' '.join(error for job in conversion['jobs'] for error in job.get('errors', []))
                    assert NEGATIVE[name] in errors and not conversion['outputs'], errors
                    assert not any((folder / 'output').rglob('*.pdf'))
                case['status'] = 'passed'
                save_json(receipt, report)
                print(f'{queue}/{name}: passed', flush=True)
        report['cross_queue'] = {}
        for name in POSITIVE:
            first, second = (report['cases'][queue][name] for queue in QUEUES)
            report['cross_queue'][name] = {'pages': [first['pages'], second['pages']],
                                            'same_page_geometry': first['page_dimensions_pt'] == second['page_dimensions_pt'],
                                            'same_page_text': first['page_text_sha256'] == second['page_text_sha256']}
            breaks = [reference['queues'][queue]['cases'][name].get('page_breaks') for queue in QUEUES]
            if all(row is not None for row in breaks):
                report['cross_queue'][name]['page_breaks'] = dict(zip(QUEUES, breaks))
            if name == 'pagination':
                report['cross_queue'][name]['grid_page_extents'] = {
                    queue: report['cases'][queue][name]['grid_page_extents'] for queue in QUEUES}
        assert sha(original) == report['original_sha256'] and win32print.GetDefaultPrinter() == default
        report.update(status='passed', original_unchanged=True, windows_default_unchanged=True)
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
    save_json(receipt, report)
    print(report['status'], report.get('error', ''), flush=True)
    return int(report['status'] != 'passed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--printers', type=Path, required=True)
    parser.add_argument('--prior', type=Path)
    parser.add_argument('--fixtures', type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--helper', choices=QUEUES)
    args = parser.parse_args()
    source, out, printers = args.source.resolve(), args.output.resolve(), args.printers.resolve()
    if not out.is_relative_to((ROOT / '.test-artifacts').resolve()) or sys.flags.optimize:
        parser.error('Use a directory in .test-artifacts and keep assertions enabled')
    if args.helper:
        helper(source, out, args.prior, read_json(printers), args.helper)
        return 0
    out.mkdir(parents=True, exist_ok=False)
    if args.fixtures:
        return verify(source, args.fixtures.resolve(), out, args.executable.resolve() if args.executable else None,
                      read_json(printers))
    if args.prior is None:
        parser.error('Provide the 2.2.26 reference fixture directory')
    prepare(source, out, args.prior.resolve(), printers)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
