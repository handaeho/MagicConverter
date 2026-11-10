"""Verify the explicit print route against an owned desktop Excel reference."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter.core.diagnostics.visual_comparison import compare, sha
from magic_converter.core.engines.office_com import ProcessLedger, application, cleanup_owned_processes
from magic_converter.core.engines.office_process import run_helper

ACTION = 'office:xlsx_print_pdf'
CASES = ('controls', 'multi-area', 'titles', 'large-print', 'large-multi', 'large-titles')
REJECTIONS = {
    'no-print-area': '직사각형 인쇄 영역',
    'too-many-areas': '직사각형 인쇄 영역',
    'too-many-cells': '10,000셀',
    'repeat-titles': '반복 인쇄',
    'outside-title-columns': '반복 인쇄',
    'multi-with-titles': '단일 인쇄 영역',
}


def save_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')


def setup(sheet, area, *, orientation, fit):
    settings = sheet.PageSetup
    settings.PrintArea = area
    settings.PaperSize = 9  # xlPaperA4
    settings.Orientation = orientation
    settings.LeftMargin = settings.RightMargin = 36
    settings.TopMargin = settings.BottomMargin = 36
    settings.CenterFooter = 'Page &P'
    settings.Zoom = False if fit else 100
    if fit:
        settings.FitToPagesWide = settings.FitToPagesTall = 1
    return {'area': settings.PrintArea, 'paper': settings.PaperSize, 'orientation': settings.Orientation,
            'fit': fit, 'zoom': settings.Zoom, 'left_margin_pt': settings.LeftMargin,
            'top_margin_pt': settings.TopMargin}


def export(book, out, name, *, copy=False):
    if copy:
        book.SaveCopyAs(str(out / (name + '.xlsx')))
    book.ExportAsFixedFormat(0, str(out / (name + '.pdf')), IgnorePrintAreas=False, OpenAfterPublish=False)


def controls(excel, out):
    book = excel.Workbooks.Add(-4167)  # xlWBATWorksheet
    try:
        sheet = book.Worksheets(1)
        sheet.Name = 'Breaks'
        sheet.Range('A1:F36').Value = tuple(tuple(f'R{r:02} C{c}' for c in range(1, 7))
                                             for r in range(1, 37))
        sheet.Range('A1:F36').RowHeight = 13
        sheet.Range('A1:F36').ColumnWidth = 12
        sheet.Range('I1').Value = 'OUTSIDE PRINT AREA'
        first = setup(sheet, '$A$1:$F$36', orientation=2, fit=False)
        sheet.HPageBreaks.Add(Before=sheet.Range('A18'))
        first['manual_break_row'] = 18
        other = book.Worksheets.Add(After=sheet)
        other.Name = 'Fit and Objects'
        other.Range('A1:F2').Merge()
        other.Range('A1').Value = 'MERGED PRINT TITLE'
        other.Range('A1:F2').Interior.Color = 0xE3ECE3
        other.Range('A1:F2').Font.Bold = True
        other.Range('A5:B11').Value = (('Period', 'Amount'), ('One', 1234.5), ('Two', 2050),
                                       ('Three', 1900), ('Four', 2150), ('Five', 2350), ('Six', 2800))
        other.Range('B6:B11').NumberFormat = '#,##0.00'
        other.Range('A1:L35').ColumnWidth = 11
        chart = other.ChartObjects().Add(150, 80, 290, 145).Chart
        chart.ChartType = 51
        chart.SetSourceData(other.Range('A5:B11'))
        chart.HasTitle = True
        chart.ChartTitle.Text = 'PRINT CHART'
        shape = other.Shapes.AddShape(1, 170, 250, 140, 30)
        shape.TextFrame.Characters().Text = 'PRINT SHAPE'
        second = setup(other, '$A$1:$L$35', orientation=1, fit=True)
        hidden = book.Worksheets.Add(After=other)
        hidden.Range('A1').Value = 'HIDDEN SHEET SENTINEL'
        hidden.Visible = 0  # xlSheetHidden
        book.SaveAs(str(out / 'controls.xlsx'), FileFormat=51)
        export(book, out, 'controls')
        sheet.PageSetup.PrintArea = ''
        book.SaveCopyAs(str(out / 'no-print-area.xlsx'))
        sheet.PageSetup.PrintArea = '$A$1:$B$4,$A$7:$B$10'
        export(book, out, 'multi-area', copy=True)
        sheet.PageSetup.PrintTitleRows = '$1:$1'
        book.SaveCopyAs(str(out / 'multi-with-titles.xlsx'))
        sheet.PageSetup.PrintTitleRows = ''
        sheet.PageSetup.PrintArea = '$A$1:$B$4,$A$7:$B$10,$A$12:$B$14'
        book.SaveCopyAs(str(out / 'too-many-areas.xlsx'))
        sheet.PageSetup.PrintArea = '$A$1:$K$1000'
        book.SaveCopyAs(str(out / 'too-many-cells.xlsx'))
        sheet.PageSetup.PrintArea = '$A$2:$F$36'
        sheet.PageSetup.PrintTitleRows = '$1:$1'
        book.SaveCopyAs(str(out / 'repeat-titles.xlsx'))
        sheet.PageSetup.PrintTitleRows = ''
        sheet.PageSetup.PrintArea = '$B$1:$F$36'
        sheet.PageSetup.PrintTitleColumns = '$A:$A'
        book.SaveCopyAs(str(out / 'outside-title-columns.xlsx'))
        return {'Breaks': first, 'Fit and Objects': second}
    finally:
        book.Close(False)


def title_controls(excel, out):
    book = excel.Workbooks.Add(-4167)
    try:
        sheet = book.Worksheets(1)
        sheet.Range('A1:H32').Value = tuple(tuple(f'R{row:02} C{col}' for col in range(1, 9))
                                             for row in range(1, 33))
        sheet.Range('A1').Value = 'TITLE CORNER'
        sheet.Range('A1:H32').ColumnWidth = 13
        sheet.Range('A1:H32').RowHeight = 14
        page = setup(sheet, '$A$1:$H$32', orientation=2, fit=False)
        sheet.HPageBreaks.Add(Before=sheet.Range('A18'))
        sheet.VPageBreaks.Add(Before=sheet.Range('F1'))
        sheet.PageSetup.PrintTitleRows = '$1:$2'
        sheet.PageSetup.PrintTitleColumns = '$A:$A'
        page.update(title_rows=sheet.PageSetup.PrintTitleRows, title_columns=sheet.PageSetup.PrintTitleColumns,
                    manual_break_row=18, manual_break_column=6)
        book.SaveAs(str(out / 'titles.xlsx'), FileFormat=51)
        export(book, out, 'titles')
        return page
    finally:
        book.Close(False)


def fixture_helper(original, out):
    ledger = ProcessLedger(out / 'fixture.processes.json')
    before = sha(original)
    started = time.monotonic()
    copy = out / 'large-print.xlsx'
    shutil.copyfile(original, copy)
    try:
        with application('excel', ledger) as excel:
            excel.DisplayAlerts = False
            reference = {'version': str(excel.Version), 'build': str(excel.Build),
                         'printer': str(excel.ActivePrinter)}
            ledger.mark('office_export', 'create controls and reference PDF')
            reference['controls'] = controls(excel, out)
            reference['titles'] = title_controls(excel, out)
            ledger.mark('office_export', 'open 40 MB private workbook copy')
            book = excel.Workbooks.Open(str(copy), UpdateLinks=0, ReadOnly=False, AddToMru=False)
            try:
                if book.ReadOnly or book.Worksheets.Count != 1:
                    raise ValueError('Expected writable single-sheet benchmark copy')
                sheet = book.Worksheets(1)
                reference['large-print'] = setup(sheet, '$A$1:$M$48', orientation=2, fit=True)
                book.Save()
                export(book, out, 'large-print')
                reference['large-multi'] = setup(sheet, '$A$1:$M$48,$A$97:$M$120', orientation=2, fit=True)
                export(book, out, 'large-multi', copy=True)
                reference['large-titles'] = setup(sheet, '$A$1:$M$48', orientation=2, fit=False)
                sheet.HPageBreaks.Add(Before=sheet.Range('A25'))
                sheet.VPageBreaks.Add(Before=sheet.Range('G1'))
                sheet.PageSetup.PrintTitleRows = '$1:$1'
                sheet.PageSetup.PrintTitleColumns = '$A:$A'
                reference['large-titles'].update(title_rows=sheet.PageSetup.PrintTitleRows,
                                                 title_columns=sheet.PageSetup.PrintTitleColumns,
                                                 manual_break_row=25, manual_break_column=7)
                export(book, out, 'large-titles', copy=True)
            finally:
                book.Close(False)
        assert sha(original) == before
        save_json(out / 'reference.json', {
            'status': 'passed', 'source_sha256': before, 'original_unchanged': True, 'excel': reference,
            'seconds': time.monotonic() - started, 'workbooks': {
                name: {'sha256': sha(out / (name + '.xlsx')), 'bytes': (out / (name + '.xlsx')).stat().st_size}
                for name in (*CASES, *REJECTIONS)},
            'reference_sha256': {name: sha(out / (name + '.pdf')) for name in CASES},
            'native_processes': ledger.owned})
    finally:
        ledger.budget.close()


def prepare(original, out):
    try:
        code, errors = run_helper([sys.executable, '-B', __file__, '--source', str(original),
                                   '--output', str(out), '--fixture-helper'],
                                  out / 'fixture.state.json', timeout=240)
        if code:
            raise RuntimeError(errors or 'Excel fixture helper failed')
    finally:
        cleanup_owned_processes(out / 'fixture.processes.json')
    receipt = json.loads((out / 'reference.json').read_text(encoding='utf-8'))
    assert receipt['status'] == 'passed' and receipt['original_unchanged']
    assert len(receipt['native_processes']) == 1
    assert receipt['native_processes'][0]['budget_phase'] == 'before_process_resume'
    print(json.dumps(receipt, ensure_ascii=False), flush=True)


def verify(original, fixtures, out, executable):
    import pymupdf as fitz
    from tools.validation.verify_large_input_rss import measure
    reference = json.loads((fixtures / 'reference.json').read_text(encoding='utf-8'))
    reference['excel']['printer'] = reference['excel']['printer'].partition(' (')[0]
    report = {'status': 'running', 'original_sha256': sha(original), 'reference': reference,
              'executable_sha256': sha(executable) if executable else None, 'cases': {}}
    receipt = out / 'report.json'
    save_json(receipt, report)
    try:
        assert reference['original_unchanged'] and report['original_sha256'] == reference['source_sha256']
        prefix = [str(executable)] if executable else [sys.executable, '-B', str(ROOT / 'run_magic_converter.py')]
        for name in (*CASES, *REJECTIONS):
            source, folder = fixtures / (name + '.xlsx'), out / name
            folder.mkdir()
            assert sha(source) == reference['workbooks'][name]['sha256']
            expected = name in CASES
            command = [*prefix, 'convert', str(source), '--action', ACTION, '--timeout', '180',
                       '--data-dir', str(out / 'profile'), '--output', str(folder / 'output'), '--json']
            temporary = folder / 'temp'
            temporary.mkdir()
            with patch.dict(os.environ, TEMP=str(temporary), TMP=str(temporary)):
                native = measure(command, folder / 'measure', expected_ok=expected, deadline=240, office_directory=folder)
            case = report['cases'][name] = {'conversion': native, 'input_sha256': sha(source)}
            assert case['input_sha256'] == reference['workbooks'][name]['sha256']
            observed = native['office_observation']['processes']
            assert len(observed) == 1 and observed[0]['application'] == 'excel'
            assert observed[0]['budget_phase'] == 'before_process_resume'
            assert observed[0]['native_commit_limit_bytes'] == 2 * 1024**3
            if expected:
                target = Path(native['outputs'][0]['path'])
                pdf = fixtures / (name + '.pdf')
                assert sha(pdf) == reference['reference_sha256'][name]
                assert any('Excel 인쇄 영역' in warning for job in native['jobs'] for warning in job.get('warnings', []))
                case['dpi'] = {str(dpi): compare(pdf, target, dpi=dpi) for dpi in (96, 144, 200)}
                assert all(run['status'] == 'matched' and all(page['mae'] == 0 for page in run['pages'])
                           for run in case['dpi'].values())
                with fitz.open(target) as doc:
                    page_text = [p.get_text() for p in doc]
                    text = '\n'.join(page_text)
                    case['pages'] = len(doc)
                    case['page_dimensions_pt'] = [[page.rect.width, page.rect.height] for page in doc]
                    if name == 'controls':
                        landscape = [page for page in doc if page.rect.width > page.rect.height]
                        assert len(doc) == 3 and len(landscape) == 2 and 'R18 C1' in landscape[1].get_text()
                        assert all(token in text for token in ('MERGED PRINT TITLE', '1,234.50', 'PRINT CHART', 'PRINT SHAPE'))
                        assert 'OUTSIDE PRINT AREA' not in text and 'HIDDEN SHEET SENTINEL' not in text
                    elif name == 'multi-area':
                        landscape = [p.get_text() for p in doc if p.rect.width > p.rect.height]
                        assert len(doc) == 3 and len(landscape) == 2
                        assert 'R01 C1' in landscape[0] and 'R07 C1' in landscape[1] and 'R05 C1' not in text
                    elif name == 'titles':
                        assert len(doc) == 4 and all('TITLE CORNER' in page for page in page_text)
                        assert sum('R01 C6' in page for page in page_text) == 2
                        assert sum('R18 C1' in page for page in page_text) == 2
                        assert sum('R18 C6' in page for page in page_text) == 1
                    elif name == 'large-print':
                        assert len(doc) == 1
                    elif name == 'large-multi':
                        assert len(doc) == 2
                    elif name == 'large-titles':
                        assert len(doc) >= 4 and all(page.strip() for page in page_text)
            else:
                errors = ' '.join(error for job in native['jobs'] for error in job.get('errors', []))
                assert REJECTIONS[name] in errors and not native['outputs'], errors
                assert not any((folder / 'output').rglob('*.pdf'))
            case['status'] = 'passed'
            save_json(receipt, report)
        assert sha(original) == report['original_sha256']
        report.update(status='passed', original_unchanged=True)
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
    save_json(receipt, report)
    print(report['status'], report.get('error', ''), flush=True)
    return int(report['status'] != 'passed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--fixtures', type=Path)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--fixture-helper', action='store_true')
    args = parser.parse_args()
    source, out = args.source.resolve(), args.output.resolve()
    if not out.is_relative_to((ROOT / '.test-artifacts').resolve()):
        parser.error('Keep Excel fixtures and outputs under .test-artifacts')
    if args.fixture_helper:
        fixture_helper(source, out)
        return 0
    out.mkdir(parents=True, exist_ok=False)
    if args.fixtures:
        return verify(source, args.fixtures.resolve(), out, args.executable.resolve() if args.executable else None)
    prepare(source, out)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
