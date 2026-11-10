"""Compare actual retained PPTX image bytes to normal PDF rendering."""
import argparse
import io
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pymupdf as fitz
from PIL import Image
from pptx import Presentation
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha
from magic_converter.core.engines.hwp_converter import run_com


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
        source = root / 'colours.pdf'
        fixtures = [('RGB', (20, 40, 180)), ('L', 60), ('CMYK', (200, 90, 10, 40)),
                    ('RGBA', (20, 40, 180, 128))]
        with fitz.open() as pdf:
            for mode, colour in fixtures:
                stream = io.BytesIO()
                Image.new(mode, (80, 60), colour).save(stream, 'PNG' if mode == 'RGBA' else 'JPEG')
                page = pdf.new_page(width=144, height=108)
                page.insert_image(page.rect, stream=stream.getvalue())
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
        if len(deck.slides) != len(fixtures):
            raise ValueError('Missing retained-image slide')
        with fitz.open(source) as pdf:
            for index, (mode, _) in enumerate(fixtures):
                slide = deck.slides[index]
                if len(slide.shapes) != 1:
                    raise ValueError('Expected exactly one retained image')
                reference = pdf[index].get_pixmap(dpi=200, alpha=True).tobytes('png')
                with Image.open(io.BytesIO(slide.shapes[0].image.blob)) as actual, Image.open(io.BytesIO(reference)) as expected:
                    matched = actual.size == expected.size and actual.tobytes() == expected.tobytes()
                report['cases'].append({'mode': mode, 'exact_pixels': matched})
        if not all(c['exact_pixels'] for c in report['cases']):
            raise ValueError('Retained image colours differ from normal PDF renderer')
        if args.office:
            native = root / 'powerpoint.pdf'
            run_com(deck_path, native, 'office_pdf')
            with fitz.open(source) as original, fitz.open(native) as result:
                if len(result) != len(original):
                    raise ValueError('Native export lost pages')
                for index, case in enumerate(report['cases']):
                    a, b = (d[index].get_pixmap(dpi=72, alpha=False).pixel(72, 54)
                            for d in (original, result))
                    delta = max(abs(x - y) for x, y in zip(a, b))
                    case.update(native_centre_delta=delta, reference_centre=a, native_centre=b)
                    if delta > 3:
                        raise ValueError('Native PDF colour changed by more than JPEG tolerance 3/255')
            report['native_office'] = 'passed'
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
