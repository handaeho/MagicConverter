"""Compare the expanded print action with Excel using the Windows default queue."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
from tempfile import TemporaryDirectory
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_excel_print_expanded import (
    ACTION, ProcessLedger, application, cleanup_owned_processes,
    compare, digest, grid, run_helper, save, save_json, setup, sha, snapshot,
)

POSITIVE = ('auto-values', 'auto-shape', 'three-areas', 'chart-only',
            'two-visible-charts', 'large-20000', 'large-20001', 'large-30000')
NEGATIVE = {'four-areas': '직사각형 인쇄 영역', 'auto-shape-far': '30,000셀',
            'large-30001': '30,000셀', 'limit-titles-over': '30,000셀',
            'outside-bottom-right': '반복 인쇄 제목'}
CASES = (*POSITIVE, *NEGATIVE)
DIRECT = (*POSITIVE, 'outside-bottom-right')


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def fixtures(excel, original, prior, out):
    previous = read(prior / 'reference.json')['workbooks']
    for name in ('auto-values', 'two-visible-charts', 'large-20000', 'large-20001'):
        source = prior / (name + '.xlsx')
        assert sha(source) == previous[name]
        shutil.copyfile(source, out / source.name)

    book = excel.Workbooks.Add(-4167)
    try:
        sheet = book.Worksheets.Item(1)
        sheet.Range('B2').Value = 'VALUE ANCHOR'
        sheet.Range('D5').Formula = '=20+22'
        setup(sheet, '', orientation=2, fit=True)
        anchor = sheet.Range('F9')
        shape = sheet.Shapes.AddShape(1, anchor.Left, anchor.Top, 124, 26)
        shape.TextFrame.Characters().Text = 'OUTSIDE SHAPE'
        save(book, out, 'auto-shape')
        shape.Top = sheet.Range('F30001').Top
        snapshot(book, out, 'auto-shape-far')
    finally:
        book.Close(False)

    book = excel.Workbooks.Add(-4167)
    try:
        sheet = book.Worksheets.Item(1)
        grid(sheet, 36, 10)
        sheet.Range('A1:J36').ColumnWidth = 11
        setup(sheet, '$B$3:$H$8,$B$13:$H$18,$B$23:$H$28', orientation=2, fit=False)
        sheet.PageSetup.PrintTitleRows = '$1:$2'
        sheet.PageSetup.PrintTitleColumns = '$A:$A'
        save(book, out, 'three-areas')
        sheet.PageSetup.PrintArea = '$B$3:$H$8,$B$13:$H$18,$B$23:$H$28,$B$32:$H$36'
        snapshot(book, out, 'four-areas')
        sheet.PageSetup.PrintArea = '$B$3:$H$26'
        sheet.PageSetup.PrintTitleRows = '$30:$30'
        sheet.PageSetup.PrintTitleColumns = '$J:$J'
        sheet.HPageBreaks.Add(Before=sheet.Range('B15'))
        snapshot(book, out, 'outside-bottom-right')
    finally:
        book.Close(False)

    book = excel.Workbooks.Add(-4167)
    try:
        sheet = book.Worksheets.Item(1)
        chart = book.Charts.Add(After=sheet)
        chart.ChartType = 51
        series = chart.SeriesCollection().NewSeries()
        series.Values = (2, 5, 9)
        series.XValues = ('ALPHA', 'BETA', 'GAMMA')
        chart.HasTitle = True
        chart.ChartTitle.Text = 'CHART ONLY SENTINEL'
        chart.PageSetup.PaperSize = 9
        sheet.Delete()
        save(book, out, 'chart-only')
    finally:
        book.Close(False)

    working = out / 'large-working.xlsx'
    shutil.copyfile(original, working)
    book = excel.Workbooks.Open(str(working), UpdateLinks=0, ReadOnly=False, AddToMru=False)
    try:
        sheet = book.Worksheets.Item(1)
        setup(sheet, '$A$1:$J$3000', orientation=2, fit=True)
        snapshot(book, out, 'large-30000')
        sheet.PageSetup.PrintArea = '$A$1:$J$3000,$A$3002'
        snapshot(book, out, 'large-30001')
        sheet.PageSetup.PrintArea = '$A$2:$J$3001'
        sheet.PageSetup.PrintTitleRows = '$1:$1'
        snapshot(book, out, 'limit-titles-over')
    finally:
        book.Close(False)
    working.unlink()


def layout(name, pdf, *, check=True):
    import pymupdf as fitz
    with fitz.open(pdf) as doc:
        text = [page.get_text() for page in doc]
        if check and name == 'auto-shape':
            assert all(token in ''.join(text) for token in ('VALUE ANCHOR', '42', 'OUTSIDE SHAPE'))
        if check and name == 'three-areas':
            assert len(text) == 3 and all('R001C02' in page for page in text)
            assert all(token in page for token, page in zip(('R003C02', 'R013C02', 'R023C02'), text))
            assert 'R009C02' not in ''.join(text) and 'R019C02' not in ''.join(text)
        if check and name == 'outside-bottom-right':
            assert len(text) == 2 and all('R030C02' not in page for page in text)
            assert 'R003C10' not in text[0] and 'R015C10' not in text[1]
        if check and name == 'chart-only':
            assert len(text) == 1 and 'CHART ONLY SENTINEL' in text[0]
        if check and name == 'two-visible-charts':
            assert len(text) == 4 and 'HIDDEN WORKSHEET SENTINEL' not in ''.join(text)
            assert 'VISIBLE CHART SHEET' in ''.join(text) and 'HIDDEN CHART SENTINEL' in ''.join(text)
        return {'pages': len(doc), 'geometry_pt': [list(page.rect)[2:] for page in doc],
                'page_text_sha256': [digest(page) for page in text]}


def prepare(original, prior, out):
    import win32print
    original_sha = sha(original)
    default = win32print.GetDefaultPrinter()
    ledger = ProcessLedger(out / 'excel.processes.json')
    references = {}
    try:
        with application('excel', ledger) as excel:
            ledger.mark('office_export', 'author validation copies')
            fixtures(excel, original, prior, out)
            printer = digest(str(excel.ActivePrinter))
            version = {'version': str(excel.Version), 'build': str(excel.Build)}
            for name in DIRECT:
                ledger.mark('office_export', name)
                book = excel.Workbooks.Open(str(out / (name + '.xlsx')), UpdateLinks=0, ReadOnly=True, AddToMru=False)
                pdf = out / (name + '.pdf')
                try:
                    assert digest(str(excel.ActivePrinter)) == printer
                    book.ExportAsFixedFormat(0, str(pdf), IgnorePrintAreas=False, OpenAfterPublish=False)
                finally:
                    book.Close(False)
                references[name] = {'pdf_sha256': sha(pdf), **layout(name, pdf)}
        assert win32print.GetDefaultPrinter() == default and sha(original) == original_sha
        save_json(out / 'reference.json', {'status': 'passed', 'original_sha256': original_sha,
                                           'default_printer_sha256': digest(default), 'excel': version,
                                           'references': references, 'native_processes': ledger.owned,
                                           'inputs': {name: sha(out / (name + '.xlsx')) for name in CASES}})
    finally:
        ledger.budget.close()


def verify(original, fixtures_dir, out, executable):
    import win32print
    from tools.validation.verify_large_input_rss import measure
    reference = read(fixtures_dir / 'reference.json')
    default = win32print.GetDefaultPrinter()
    before = sha(original)
    assert before == reference['original_sha256'] and digest(default) == reference['default_printer_sha256']
    prefix = [str(executable)] if executable else [sys.executable, '-B', str(ROOT / 'run_magic_converter.py')]
    results = {}
    receipt = out / 'report.json'
    report = {'status': 'running', 'original_sha256': before, 'reference': reference, 'cases': results,
              'executable_sha256': sha(executable) if executable else None}
    save_json(receipt, report)
    try:
        for name in CASES:
            case_dir = out / name
            case_dir.mkdir()
            source = fixtures_dir / (name + '.xlsx')
            assert sha(source) == reference['inputs'][name]
            cmd = [*prefix, 'convert', str(source), '--action', ACTION, '--timeout', '180',
                   '--data-dir', str(out / 'profile'), '--output', str(case_dir / 'output'), '--json']
            with TemporaryDirectory(prefix='ep-', dir=ROOT / '.test-artifacts') as temp:
                with patch.dict(os.environ, TEMP=temp, TMP=temp):
                    assert not os.environ.get('MAGIC_CONVERTER_EXCEL_VALIDATION_PRINTER')
                    conversion = measure(cmd, case_dir / 'measure', expected_ok=name in POSITIVE,
                                         deadline=240, office_directory=temp)
            case = results[name] = {'status': 'running', 'conversion': conversion}
            save_json(receipt, report)
            process, = conversion['office_observation']['processes']
            assert process['native_commit_limit_bytes'] == 2 * 1024**3 and process['budget_phase'] == 'before_process_resume'
            assert not conversion['lingering_owned_pids'] and not conversion['office_observation']['lingering_before_verifier_cleanup']
            assert win32print.GetDefaultPrinter() == default and sha(source) == reference['inputs'][name]
            if name in POSITIVE:
                pdf = fixtures_dir / (name + '.pdf')
                assert sha(pdf) == reference['references'][name]['pdf_sha256']
                output, = conversion['outputs']
                assert conversion['seconds'] < 180 and 0 < output['bytes'] <= 256 * 1024**2
                case['dpi'] = {str(dpi): compare(pdf, output['path'], dpi=dpi) for dpi in (96, 144, 200)}
                assert all(row['status'] == 'matched' and all(page['mae'] == 0 for page in row['pages'])
                           for row in case['dpi'].values()), name
                case.update(layout(name, output['path']))
            else:
                errors = ' '.join(error for job in conversion['jobs'] for error in job.get('errors', []))
                assert NEGATIVE[name] in errors and not conversion['outputs'], errors
                assert not any((case_dir / 'output').rglob('*.pdf'))
                if name == 'outside-bottom-right':
                    pdf = fixtures_dir / (name + '.pdf')
                    assert sha(pdf) == reference['references'][name]['pdf_sha256']
                    case['direct_excel_omitted_titles'] = layout(name, pdf)['pages']
            case['status'] = 'passed'
            save_json(receipt, report)
            print(f'{name}: passed', flush=True)
        assert sha(original) == before
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
    parser.add_argument('--prior', type=Path)
    parser.add_argument('--fixtures', type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--helper', action='store_true')
    args = parser.parse_args()
    original, out = args.source.resolve(), args.output.resolve()
    if sys.flags.optimize or not out.is_relative_to((ROOT / '.test-artifacts').resolve()):
        parser.error('Use .test-artifacts and keep assertions enabled')
    if args.helper:
        prepare(original, args.prior.resolve(), out)
        return 0
    out.mkdir(parents=True, exist_ok=False)
    if args.fixtures:
        return verify(original, args.fixtures.resolve(), out, args.executable.resolve() if args.executable else None)
    try:
        code, errors = run_helper([sys.executable, '-B', __file__, '--source', str(original), '--output', str(out),
                                   '--prior', str(args.prior.resolve()), '--helper'], out / 'excel.state.json', timeout=300)
        if code:
            raise RuntimeError(errors or 'Excel reference helper failed')
    finally:
        cleanup_owned_processes(out / 'excel.processes.json')
    print('Native Excel references saved', flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
