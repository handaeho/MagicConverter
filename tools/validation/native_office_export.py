"""Direct source/frozen native export for visual verification, without fallback."""
import json
from pathlib import Path
import sys

from magic_converter.core.engines.office_com import cleanup_owned_processes
from magic_converter.core.engines.office_process import run_helper


def export_pdf(source, destination, folder, executable=None):
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=False)
    request, report = folder / 'request.json', folder / 'result.json'
    request.write_text(json.dumps({'operation': 'office_pdf', 'source': str(Path(source).resolve()),
                                  'destination': str(Path(destination).resolve())}), encoding='utf-8')
    prefix = ([str(Path(executable).resolve()), '--office-com'] if executable else
              [sys.executable, '-B', '-m', 'magic_converter.core.engines.office_com'])
    try:
        code, errors = run_helper([*prefix, str(request), str(report)],
                                  report.with_suffix('.state.json'), timeout=180)
        result = json.loads(report.read_text(encoding='utf-8')) if report.exists() else {}
        if code or result.get('ok') is not True or not Path(destination).is_file():
            raise RuntimeError(result.get('error') or errors or 'Native Office export failed')
        identities = json.loads(report.with_suffix('.processes.json').read_text(encoding='utf-8'))['owned']
        if len(identities) != 1 or any(item.get('budget_phase') != 'before_process_resume'
                                     or item.get('commit_limit_bytes') != 2 * 1024**3 for item in identities):
            raise RuntimeError('Missing direct native pre-resume budget receipt')
        return {'route': 'direct frozen --office-com' if executable else 'direct source office_com',
                'native_processes': identities}
    finally:
        cleanup_owned_processes(report.with_suffix('.processes.json'))
