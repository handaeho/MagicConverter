"""Resolve two installed queues in a private Excel session without changing the default."""
import argparse
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_excel_print_expanded import (
    ProcessLedger, application, cleanup_owned_processes, digest, run_helper, save_json,
)


def probe(out, primary, secondary):
    import win32print
    import winreg
    from pywintypes import com_error

    default = win32print.GetDefaultPrinter()
    names = [item['pPrinterName'] for item in win32print.EnumPrinters(6, None, 4)]
    queues = {}
    for label, match in (('samsung', primary), ('microsoft-pdf', secondary)):
        queues[label], = [name for name in names if match.casefold() in name.casefold()]
    assert len(set(queues.values())) == 2
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Microsoft\Windows NT\CurrentVersion\Devices') as key:
        ports = {name: winreg.QueryValueEx(key, name)[0].split(',')[1] for name in {default, *queues.values()}}
    ledger = ProcessLedger(out / 'printer.processes.json')
    try:
        with application('excel', ledger) as excel:
            initial = str(excel.ActivePrinter)
            assert default in initial and ports[default] in initial
            book = excel.Workbooks.Add(-4167)
            result = {}
            try:
                for label, name in queues.items():
                    localized = initial.replace(default, name).replace(ports[default], ports[name])
                    for request in (localized, f'{name} on {ports[name]}'):
                        try:
                            excel.ActivePrinter = request
                        except com_error:
                            continue
                        actual = str(excel.ActivePrinter)
                        assert name in actual and win32print.GetDefaultPrinter() == default
                        result[label] = {'request': request, 'active_printer': actual, 'sha256': digest(actual)}
                        break
                    else:
                        raise ValueError(f'Excel did not select {label}')
            finally:
                book.Close(False)
        assert win32print.GetDefaultPrinter() == default
        save_json(out / 'printers.json', result)
    finally:
        ledger.budget.close()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--primary', default='Samsung X3220NR')
    parser.add_argument('--secondary', default='Microsoft Print to PDF')
    parser.add_argument('--helper', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve()
    if not out.is_relative_to((ROOT / '.test-artifacts').resolve()) or sys.flags.optimize:
        parser.error('Keep local printer names under .test-artifacts and leave assertions enabled')
    if args.helper:
        probe(out, args.primary, args.secondary)
        return
    out.mkdir(parents=True, exist_ok=False)
    try:
        code, errors = run_helper([sys.executable, '-B', __file__, '--output', str(out), '--primary', args.primary,
                                   '--secondary', args.secondary, '--helper'], out / 'printer.state.json', timeout=90)
        if code:
            raise RuntimeError(errors)
    finally:
        cleanup_owned_processes(out / 'printer.processes.json')
    print('Excel selected both queues and Windows default was preserved.', flush=True)


if __name__ == '__main__':
    main()
