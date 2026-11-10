"""Native alpha fidelity, deliberately wrong alpha, and deletion controls."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pymupdf as fitz
from pptx import Presentation
from PIL import Image, ImageChops, ImageStat
from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import glyph_alpha_pdf
from magic_converter.core.diagnostics.visual_comparison import compare, sha
from tools.validation.native_office_export import export_pdf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--office', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    report = {'version': __version__, 'status': 'running', 'dpi': [96, 144],
              'executable_sha256': sha(args.executable) if args.executable else None,
              'mae_ceiling': 1.5, 'renderer': 'pinned PyMuPDF; Poppler unavailable'}
    report['native_office'] = 'pending' if args.office else 'skipped'
    def save():
        (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    try:
        source = out/'alpha.pdf'; glyph_alpha_pdf(source); before = sha(source)
        env = {**os.environ, 'PYTHONUTF8': '1', 'PATH': str(Path(os.environ['SystemRoot'])/'System32')}
        for key in ('PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
            env.pop(key, None)
        prefix = [str(args.executable.resolve())] if args.executable else [sys.executable, str(ROOT/'run_magic_converter.py')]
        job_path = out/'job.json'
        done = subprocess.run(prefix+['convert', str(source), '--action', 'pptx_editable', '--output', str(out/'output'),
            '--report', str(job_path), '--data-dir', str(out/'profile'), '--timeout', '60', '--json'],
            env=env, capture_output=True, timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        job = json.loads(job_path.read_text(encoding='utf-8-sig'))
        if done.returncode or not job['ok']: raise ValueError(str(job))
        target = Path(job['jobs'][0]['outputs'][0]); deck = Presentation(target)
        shapes = deck.slides[0].shapes
        groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
        alpha = [int(a.get('val')) for g in groups for s in g.shapes for a in s._element.xpath('.//a:alpha')]
        if len(groups) != 2 or alpha != [25000, 50000] or sum(s.shape_type == 13 for s in shapes) != 1:
            raise ValueError('Alpha or independent outline structure lost')
        texts = [s for s in shapes if s.has_text_frame and s.text]
        if [s.text for s in texts] != ['Independent title']:
            raise ValueError('Ordinary alpha text lost character editing')
        ordinary_alpha = [int(a.get('val')) for s in texts for a in s._element.xpath('.//a:alpha')]
        if not ordinary_alpha or not all(49800 < a < 50300 for a in ordinary_alpha):
            raise ValueError('Existing ordinary text alpha was not preserved')
        report['ordinary_alpha_text'] = 'editable_and_preserved'
        report['structure'] = 'passed'
        if not args.office:
            if sha(source) != before: raise ValueError('Original changed')
            report.update(status='passed', source_sha256=before,
                          note='Structure only; native pixels/deletion not requested')
            save(); print(json.dumps(report, ensure_ascii=False, indent=2)); return 0
        native = out/'native.pdf'
        report['native_export'] = export_pdf(target, native, out/'native-export', args.executable)
        report['visual'] = {}
        for dpi in (96, 144):
            result = compare(source, native, dpi=dpi, artifacts=out/f'visual-{dpi}')
            report['visual'][str(dpi)] = result
            if result['pages'][0]['mae'] >= report['mae_ceiling']: raise ValueError('Alpha pixels exceeded fixed glyph ceiling')
        # A valid file with all alphas removed must be rejected by the same gate.
        for g in groups:
            for s in g.shapes:
                for a in s._element.xpath('.//a:alpha'): a.getparent().remove(a)
        wrong = out/'wrong-alpha.pptx'; deck.save(wrong)
        report['negative_export'] = export_pdf(wrong, out/'wrong-alpha.pdf', out/'negative-export', args.executable)
        report['negative_visual'] = compare(source, out/'wrong-alpha.pdf')
        if report['negative_visual']['pages'][0]['mae'] < report['mae_ceiling']:
            raise ValueError('Wrong-alpha negative control escaped pixel gate')
        for g in groups: g._element.getparent().remove(g._element)
        for shape in list(shapes):
            if shape.shape_type == 13: shape._element.getparent().remove(shape._element)
        deleted = out/'deleted.pptx'; deck.save(deleted)
        report['deletion_export'] = export_pdf(deleted, out/'deleted.pdf', out/'deletion-export', args.executable)
        with fitz.open(out/'deleted.pdf') as pdf:
            page = pdf[0]
            pix = page.get_pixmap(clip=fitz.Rect(25, 70, 375, 250), colorspace=fitz.csRGB)
            if any(abs(value-(204, 230, 255)[index % 3]) > 3 for index, value in enumerate(pix.samples)):
                raise ValueError('Deleted glyph left ink or backdrop changed')
        def title_pixels(path):
            with fitz.open(path) as pdf:
                pix = pdf[0].get_pixmap(dpi=144, clip=fitz.Rect(20, 10, 150, 45), colorspace=fitz.csRGB)
                return Image.frombytes('RGB', (pix.width, pix.height), pix.samples)
        before_title = title_pixels(native)
        after_title = title_pixels(out/'deleted.pdf')
        # Office exports translucent text as paths. Searchability of that PDF
        # is not proof of whether the still-editable PPTX title survived.
        if ImageChops.difference(before_title, after_title).getbbox() is not None:
            raise ValueError('Deleting other regions changed independent title pixels')
        report['independent_title_pixels'] = 'byte_identical_before_after_deletion'
        report['source_title_roi_mae'] = sum(ImageStat.Stat(ImageChops.difference(
            title_pixels(source), before_title)).mean) / 3
        texts[0]._element.getparent().remove(texts[0]._element)
        missing = out/'missing-title.pptx'; deck.save(missing)
        report['missing_title_export'] = export_pdf(missing, out/'missing-title.pdf', out/'missing-title-export', args.executable)
        missing_mae = sum(ImageStat.Stat(ImageChops.difference(
            before_title, title_pixels(out/'missing-title.pdf'))).mean) / 3
        report['missing_title_roi_mae'] = missing_mae
        if missing_mae < report['mae_ceiling']:
            raise ValueError('Missing-title negative control escaped the visual gate')
        if sha(source) != before: raise ValueError('Original changed')
        report.update(status='passed', native_office='passed', source_sha256=before, deletion='passed', negative_alpha='rejected')
    except Exception as exc:
        report.update(status='failed', error=type(exc).__name__+': '+str(exc))
    save(); print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
