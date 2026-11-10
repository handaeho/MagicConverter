"""Spaces with proved empty outlines and visible whitespace-map controls."""
import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pymupdf as fitz
from pptx import Presentation
from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import clipped_sequence_pdf
from magic_converter.core.diagnostics.visual_comparison import compare, sha

CASES = ('constant-space', 'gradient-space', 'partial-space', 'visible-space')


def fixture(path, case):
    clipped_sequence_pdf(path, '20 110 12 30' if case == 'partial-space' else '0 0 360 240',
                         mask='gradient' if case == 'gradient-space' else True)
    if case == 'visible-space':
        from fontTools.ttLib import TTFont
        from magic_converter.core.diagnostics.special_diagnostics import sample_font
        with tempfile.TemporaryDirectory() as folder:
            font_path = Path(folder) / 'visible-space.ttf'
            sample_font(font_path)
            with TTFont(font_path) as font:
                for table in font['cmap'].tables:
                    if table.isUnicode():
                        table.cmap[32] = 'A'
                font.save(font_path)
            with fitz.open(path) as pdf:
                page = pdf[0]
                page.insert_font(fontname='proof', fontfile=str(font_path))
                for xref in page.get_contents():
                    pdf.update_stream(xref, pdf.xref_stream(xref).replace(
                        b'(Clipped text) Tj', b'() Tj'))
                # Let MuPDF encode the Type0 font's actual CIDs; literal PDF
                # bytes would not represent the embedded Unicode font.
                page.insert_text((20, 120), 'A A', fontname='proof', fontsize=20)
                xref = page.get_contents()[-1]
                data = pdf.xref_stream(xref)
                pdf.update_stream(xref, b'q /SM gs\n' + data + b'\nQ')
                pdf.saveIncr()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--executable', type=Path)
    p.add_argument('--baseline', type=Path)
    p.add_argument('--office', action='store_true')
    a = p.parse_args(); out = a.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'version': __version__, 'cases': {},
              'executable_sha256': sha(a.executable) if a.executable else None}
    def save():
        (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    def convert(source, folder, executable):
        prefix = [str(executable.resolve())] if executable else [sys.executable, '-B', str(ROOT / 'run_magic_converter.py')]
        result_path = folder.with_suffix('.json')
        result = subprocess.run(prefix + ['convert', str(source), '--action', 'pptx_editable', '--output', str(folder),
            '--data-dir', str(out / 'profile'), '--report', str(result_path), '--json', '--timeout', '60'],
            capture_output=True, timeout=120, env={**os.environ, 'PYTHONUTF8': '1'},
            creationflags=subprocess.CREATE_NO_WINDOW)
        job = json.loads(result_path.read_text(encoding='utf-8-sig'))
        assert result.returncode == 0 and job['ok'], job
        return Path(job['jobs'][0]['outputs'][0])
    save()
    try:
        for name in CASES:
            folder = out / name; folder.mkdir(); source = folder / 'source.pdf'; fixture(source, name)
            before = sha(source); target = convert(source, folder / 'converted', a.executable)
            deck = Presentation(target); shapes = deck.slides[0].shapes
            groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
            pictures = sum(s.shape_type == 13 for s in shapes)
            editable = name in {'constant-space', 'visible-space'}
            assert (pictures == 0 and len(groups) == 1) if editable else (pictures == 1 and not groups)
            assert 'Independent title' in [s.text for s in shapes if s.has_text_frame]
            case = {'status': 'running', 'input_sha256': before, 'pictures': pictures, 'groups': len(groups)}
            if editable:
                assert groups[0].shapes[0]._element.xpath('.//a:alpha')[0].get('val') == '50000'
                if name == 'visible-space':
                    assert len(groups[0].shapes[0]._element.xpath('.//a:moveTo')) == 3
            report['cases'][name] = case; save()
            if a.office:
                from tools.validation.native_office_export import export_pdf
                native = folder / 'native.pdf'
                case['native_export'] = export_pdf(target, native, folder / 'native-export', a.executable)
                case['dpi'] = {str(dpi): compare(source, native, dpi=dpi)['pages'][0]
                               for dpi in (72, 96, 144, 200, 300)}
                assert all(item.get('mae', 255) < 1 for item in case['dpi'].values())
                if a.baseline:
                    previous = convert(source, folder / 'baseline', a.baseline)
                    previous_pdf = folder / 'baseline.pdf'
                    case['baseline_export'] = export_pdf(previous, previous_pdf, folder / 'baseline-export', a.baseline)
                    old_shapes = Presentation(previous).slides[0].shapes
                    case['baseline_pictures'] = sum(s.shape_type == 13 for s in old_shapes)
                    if name == 'constant-space':
                        assert case['baseline_pictures'] == 1
                    elif not editable:
                        case['baseline_output_dpi'] = {str(dpi): compare(previous_pdf, native, dpi=dpi)['pages'][0]
                                                       for dpi in (72, 96, 144, 200, 300)}
                        assert all(item.get('mae') == 0 for item in case['baseline_output_dpi'].values())
                for shape in list(shapes):
                    if shape.shape_type == 13 or shape in groups:
                        shape._element.getparent().remove(shape._element)
                deleted = folder / 'deleted.pptx'; deck.save(deleted); deleted_pdf = folder / 'deleted.pdf'
                case['deletion_export'] = export_pdf(deleted, deleted_pdf, folder / 'deletion-export', a.executable)
                with fitz.open(deleted_pdf) as pdf:
                    assert 'Independent title' in pdf[0].get_text()
                    assert min(pdf[0].get_pixmap(clip=fitz.Rect(0, 60, 200, 180)).samples) >= 250
            assert sha(source) == before
            case['status'] = 'passed'; save()
        report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
    save(); print(report['status']); return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
