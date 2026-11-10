"""Source/frozen CJK anchors versus an intentionally unanchored Office control."""
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
from pptx.util import Pt
from magic_converter import __version__
from magic_converter.core.pdf.pdf_fonts import resolve_font
from magic_converter.core.pdf.pdf_layout import text_spans
from magic_converter.core.conversion.converter import _add_text_region
from magic_converter.core.diagnostics.visual_comparison import compare, sha
from tools.validation.native_office_export import export_pdf


def characters(page):
    return [c for s in text_spans(page) for c in s.characters if not c.text.isspace()]


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--executable', type=Path)
    args = p.parse_args(); root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'version': __version__,
              'executable_sha256': sha(args.executable) if args.executable else None,
              'scope': 'CJK source anchors, right-angle rotation and ordinary Latin control; real corpus measured separately.',
              'negative_control': 'Validation-authored single-region deck, exported by the same native helper.'}
    def save(): (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    try:
        face = resolve_font('Malgun Gothic')
        if face is None: raise RuntimeError('Required fixture font Malgun Gothic is unavailable')
        source = root/'source.pdf'
        with fitz.open() as pdf:
            page = pdf.new_page(width=560, height=560)
            page.insert_font(fontname='local', fontfile=str(face.path))
            for index, c in enumerate('가나다라마바사아자차카타파하' * 3):
                page.insert_text((30 + index * 11.065, 80), c, fontname='local', fontsize=11.03)
            page = pdf.new_page(width=560, height=560)
            page.insert_text((30,80), 'Ordinary Latin words stay editable', fontsize=12)
            page = pdf.new_page(width=560, height=560)
            page.insert_font(fontname='local', fontfile=str(face.path))
            for index, c in enumerate('가나다라마바사아자차카타파하' * 3):
                page.insert_text((80,520-index*11.065),c,fontname='local',fontsize=11.03,rotate=90)
            pdf.save(source)
        before = sha(source)
        prefix = [str(args.executable.resolve())] if args.executable else [sys.executable, str(ROOT/'run_magic_converter.py')]
        result_path = root/'conversion.json'
        done = subprocess.run([*prefix, 'convert', str(source), '--action', 'pptx_editable', '--output', str(root/'output'),
                               '--report', str(result_path), '--data-dir', str(root/'profile'), '--timeout', '60', '--json'],
                              capture_output=True, timeout=120, env={**os.environ, 'PYTHONUTF8': '1'},
                              creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        job = json.loads(result_path.read_text(encoding='utf-8-sig'))
        if done.returncode or not job['ok']: raise ValueError(str(job))
        target = Path(job['jobs'][0]['outputs'][0]); deck = Presentation(target)
        if not all(s.has_text_frame for slide in deck.slides for s in slide.shapes): raise ValueError('CJK text became a picture')
        if len(deck.slides[0].shapes) < 2 or len(deck.slides[1].shapes) != 1: raise ValueError('CJK/Latin region controls differ')
        report['native_export'] = export_pdf(target, root/'native.pdf', root/'native-export', args.executable)
        negative = Presentation(); negative.slide_width = Pt(560); negative.slide_height = Pt(560)
        with fitz.open(source) as pdf:
            for page in pdf:
                slide = negative.slides.add_slide(negative.slide_layouts[6])
                for span in text_spans(page): _add_text_region(slide, span, 1, 0, 0)
        negative.save(root/'unanchored.pptx')
        report['unanchored_export'] = export_pdf(root/'unanchored.pptx', root/'unanchored.pdf', root/'unanchored-export', args.executable)
        with fitz.open(source) as a, fitz.open(root/'native.pdf') as b, fitz.open(root/'unanchored.pdf') as c:
            metrics = []
            for index in range(3):
                expected, actual, control = characters(a[index]), characters(b[index]), characters(c[index])
                if [v.text for v in expected] != [v.text for v in actual] or [v.text for v in expected] != [v.text for v in control]:
                    raise ValueError('Native searchable characters differ')
                metrics.append({'page':index+1, 'max_origin_error_pt': max(abs(fitz.Point(x.origin)-fitz.Point(y.origin)) for x,y in zip(expected,actual)),
                                'unanchored_max_origin_error_pt': max(abs(fitz.Point(x.origin)-fitz.Point(y.origin)) for x,y in zip(expected,control))})
        report['positions'] = metrics
        if metrics[0]['max_origin_error_pt'] >= .6 or metrics[0]['unanchored_max_origin_error_pt'] <= .7:
            raise ValueError('CJK anchor/negative drift control failed')
        if metrics[1]['max_origin_error_pt'] >= .6: raise ValueError('Latin control moved')
        if metrics[2]['max_origin_error_pt'] >= .6: raise ValueError('Rotated CJK control moved')
        report['visual'] = compare(source, root/'native.pdf', artifacts=root/'visual')
        report['unanchored_visual'] = compare(source, root/'unanchored.pdf', artifacts=root/'unanchored-visual')
        if sha(source) != before: raise ValueError('Original changed')
        report.update(status='passed', source_sha256=before)
    except Exception as exc: report.update(status='failed', error=type(exc).__name__+': '+str(exc))
    save(); print(json.dumps(report, ensure_ascii=True), flush=True)
    return 0 if report['status']=='passed' else 1


if __name__ == '__main__': raise SystemExit(main())
