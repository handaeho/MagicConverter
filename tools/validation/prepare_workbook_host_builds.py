"""Prepare isolated workbook diagnostics sources from the verified releases."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
from zipfile import ZipFile


ROOT = Path(__file__).resolve().parents[2]
PHASE_FILES = ('run_magic_converter.py', 'magic_converter/cli.py',
               'magic_converter/core/documents/large_spreadsheet.py', 'magic_converter/core/jobs/phase_trace.py')
WORKBOOK = PHASE_FILES[2]
OLDER_PHASE_CONTROL = '289043481c7f703b28914053df85e55bf1332199'
SEGMENT = re.compile(r'            compact=chunk_path\n.*?(?=            merged_pages\+=chunk_pages)', re.S)


def instrument(block):
    """Insert clocks between the original MuPDF calls without changing them."""
    checkpoint = 'if trace: checkpoints.append((perf_counter_ns(), time.thread_time_ns()))'
    lines = []
    for line in block.splitlines():
        lines.append(line)
        statement = line.strip()
        if statement == 'compact=chunk_path':
            lines.append('            checkpoints = [(perf_counter_ns(), time.thread_time_ns())] if trace else None')
        elif statement in ('writer.subset_fonts()', 'writer.close();writer=None', 'combined.saveIncr()'):
            # The last point goes outside both PDF contexts, including their close cost.
            if statement != 'combined.saveIncr()':
                lines.append('            ' + checkpoint)
        elif statement.startswith('with fitz.open(compact)'):
            lines.append('                        ' + checkpoint)
        elif statement.startswith('combined.insert_pdf('):
            lines.append('                        ' + checkpoint)
        elif statement == 'else:shutil.copyfile(compact,destination)':
            lines.extend(('                ' + checkpoint, lines.pop().replace('else:', 'else:\n                '),
                          '                ' + checkpoint))
    lines.extend(('            if trace:',
                  '                mark("workbook_chunk", chunk=chunk_number, pages=chunk_pages,',
                  '                     merged_before=merged_pages, clocks=checkpoints)'))
    if sum('checkpoints' in line for line in lines) != 8:
        raise ValueError('Unexpected workbook save implementation')
    return '\n'.join(lines) + '\n'


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--old-hooks', type=Path, required=True,
                   help='Previously verified 2.2.23 tracing-only snapshot')
    args = p.parse_args()
    out = args.output.resolve()
    if not out.is_relative_to((ROOT / '.test-artifacts').resolve()):
        p.error('Keep diagnostics under .test-artifacts')
    out.mkdir(parents=True, exist_ok=False)
    sources = {}
    for version in ('2.2.21', '2.2.22', '2.2.25'):
        old = json.loads((ROOT / 'dist' / f'v{version}' / 'validation/summary.json').read_text(encoding='utf-8'))
        commit = old['tested_commit']
        folder = out / ('v' + version)
        archive = subprocess.check_output(['git', 'archive', '--format=zip', commit], cwd=ROOT)
        with ZipFile(io.BytesIO(archive)) as bundle:
            for entry in bundle.infolist():
                if not entry.is_dir():
                    target = folder / entry.filename
                    if not target.resolve().is_relative_to(folder):
                        raise ValueError('Invalid path in Git archive')
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(bundle.read(entry))
        changed = {WORKBOOK}
        if version != '2.2.25':
            if subprocess.check_output(['git', 'diff', '--name-only', commit, OLDER_PHASE_CONTROL,
                                        '--', *PHASE_FILES[:-1]], cwd=ROOT):
                raise ValueError('Historical CLI is incompatible with the prior tracing hooks')
            expected = old['flow']['executable_sha256']['cli']
            old_receipt = json.loads((ROOT / 'dist/v2.2.25/validation/historical/history-analysis.json')
                                     .read_text(encoding='utf-8'))['source_receipts'][version]['instrumentation']
            for name in PHASE_FILES:
                data = (args.old_hooks / name).read_bytes()
                if hashlib.sha256(data).hexdigest() != old_receipt[name]:
                    raise ValueError(f'Historical hook checksum differs: {name}')
                (folder / name).write_bytes(data)
            changed.update(PHASE_FILES)
        else:
            expected = old['flow']['executable_sha256']['cli']
        path = folder / WORKBOOK
        raw = path.read_text(encoding='utf-8')
        updated, count = SEGMENT.subn(lambda match: instrument(match.group()), raw)
        if count != 1:
            raise ValueError('Expected one workbook save function')
        path.write_text(updated, encoding='utf-8', newline='\n')
        compile(updated, str(path), 'exec')
        sources[version] = {'commit': commit, 'directory': str(folder), 'published_sha256': expected,
                            'diagnostic_sources': {name: hashlib.sha256((folder / name).read_bytes()).hexdigest()
                                                   for name in sorted(changed)}}
    (out / 'sources.json').write_text(json.dumps(sources, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
