"""Check actual source/frozen text rotations and optional native PDF text."""
import argparse
import json
import math
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pymupdf as fitz
from pptx import Presentation
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha
from magic_converter.core.engines.hwp_converter import run_com
from magic_converter.core.pdf.pdf_fonts import resolve_font
from magic_converter.core.pdf.pdf_layout import text_spans


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--office', action='store_true')
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'version': __version__, 'cases': [],
              'executable_sha256': sha(args.executable) if args.executable else None,
              'native_office': 'pending' if args.office else 'skipped'}
    path = root / 'report.json'
    def save():
        path.write_text(json.dumps(report, indent=2), encoding='utf-8')
    save()
    try:
        source = root / 'directions.pdf'
        face = resolve_font('Arial')
        if face is None:
            raise ValueError('Arial is required for this controlled native font comparison')
        with fitz.open() as pdf:
            for matrix in (fitz.Matrix(1.004, 1), fitz.Matrix(.02), fitz.Matrix(90)):
                page = pdf.new_page(width=400, height=400)
                page.insert_font(fontname='local', fontfile=str(face.path))
                page.insert_text((80, 260), 'Direction 0123', fontsize=24, fontname='local',
                                 morph=(fitz.Point(80, 260), matrix))
            pdf.save(source)
        original_hash = sha(source)
        env = os.environ.copy()
        env['PYTHONUTF8'] = '1'
        env['PATH'] = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32')
        for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
            env.pop(key, None)
        prefix = [str(args.executable.resolve())] if args.executable else [sys.executable, str(ROOT / 'run_magic_converter.py')]
        job_report = root / 'conversion.json'
        done = subprocess.run(prefix + ['convert', str(source), '--action', 'pptx_editable', '--output',
                              str(root / 'editable'), '--report', str(job_report), '--data-dir',
                              str(root / 'profile'), '--timeout', '60', '--json'], env=env,
                              capture_output=True, timeout=120,
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        job = json.loads(job_report.read_text(encoding='utf-8-sig'))
        if done.returncode or not job['ok']:
            raise RuntimeError(str(job))
        deck_path = Path(job['jobs'][0]['outputs'][0])
        deck = Presentation(deck_path)
        if len(deck.slides) != 3:
            raise ValueError('Missing text direction page')
        with fitz.open(source) as pdf:
            for index, page in enumerate(pdf):
                spans = text_spans(page)
                direction = spans[0].direction
                expected = math.degrees(math.atan2(direction[1], direction[0])) % 360
                shapes = [s for s in deck.slides[index].shapes if s.has_text_frame]
                if len(shapes) != 1:
                    raise ValueError('Expected exactly one editable textbox')
                actual = shapes[0].rotation
                error = abs((actual - expected + 180) % 360 - 180)
                report['cases'].append({'page': index + 1, 'expected_angle': expected, 'actual_angle': actual,
                                        'angle_error': error, 'matched': error <= 1 / 60000})
        if not all(c['matched'] for c in report['cases']):
            raise ValueError('Recovered quad slope changed the PDF baseline direction')
        # A saved/reopened result must remain editable, including real rotations.
        deck.slides[0].shapes[0].text = 'Edited direction'
        edited = root / 'edited.pptx'
        deck.save(edited)
        if Presentation(edited).slides[0].shapes[0].text != 'Edited direction':
            raise ValueError('Text edit did not survive save/reopen')
        report['edit_roundtrip'] = 'passed'
        if args.office:
            native = root / 'powerpoint.pdf'
            run_com(deck_path, native, 'office_pdf')
            with fitz.open(source) as original, fitz.open(native) as result:
                if len(result) != len(original):
                    raise ValueError('Native PDF lost pages')
                def characters(page):
                    return [c for s in text_spans(page) for c in s.characters if not c.text.isspace()]
                before, after = characters(original[0]), characters(result[0])
                if [c.text for c in before] != [c.text for c in after]:
                    raise ValueError('Horizontal native PDF lost searchable text')
                delta = max(max(abs(a.origin[k] - b.origin[k]) for k in (0, 1)) for a, b in zip(before, after))
                if delta > .6:
                    raise ValueError('Native PDF character position moved more than .6 pt')
                report.update(native_office='passed', native_searchable_characters=len(after),
                              max_character_position_delta_pt=delta)
        if sha(source) != original_hash:
            raise ValueError('Source changed')
        report.update(status='passed', source_sha256=original_hash)
    except Exception as error:
        report.update(status='failed', error=str(error))
    save()
    print(json.dumps(report, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
