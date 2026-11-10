"""Actual Word origins for inherited tracking and scale, with prior EXE controls."""
import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha


def fixture(path, case):
    from docx import Document
    from docx.enum.style import WD_STYLE_TYPE
    from docx.shared import Pt
    from docx.oxml import OxmlElement
    from docx.oxml.ns import qn
    def metrics(node, spacing, width):
        for tag, value in (('spacing', spacing), ('w', width)):
            item = OxmlElement('w:' + tag); item.set(qn('w:val'), str(value)); node.append(item)
    doc = Document()
    p = doc.add_paragraph(); run = p.add_run('가나다라마바사아자차카타파하')
    run.font.name = '굴림'; run.font.size = Pt(12)
    run._r.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), '굴림')
    if case in {'character', 'zero'}:
        parent = doc.styles.add_style('MetricParent', WD_STYLE_TYPE.CHARACTER)
        metrics(parent.element.get_or_add_rPr(), 24, 100)
        child = doc.styles.add_style('MetricChild', WD_STYLE_TYPE.CHARACTER)
        child.base_style = parent; run.style = child
        if case == 'zero':
            metrics(run._r.get_or_add_rPr(), 0, 100)
    elif case == 'paragraph':
        parent = doc.styles.add_style('ParaMetricParent', WD_STYLE_TYPE.PARAGRAPH)
        metrics(parent.element.get_or_add_rPr(), -12, 100)
        child = doc.styles.add_style('ParaMetricChild', WD_STYLE_TYPE.PARAGRAPH)
        child.base_style = parent; p.style = child
    elif case == 'defaults':
        node = doc.styles.element.find(qn('w:docDefaults')).find(qn('w:rPrDefault')).find(qn('w:rPr'))
        metrics(node, 12, 100)
    elif case == 'scale':
        parent = doc.styles.add_style('ScaleParent', WD_STYLE_TYPE.PARAGRAPH)
        metrics(parent.element.get_or_add_rPr(), 0, 110)
        p.style = parent
    doc.save(path)
    return {'character': (1.2, 100), 'zero': (0, 100), 'paragraph': (-.6, 100),
            'defaults': (.6, 100), 'scale': (0, 110)}[case]


def origins(path):
    import pymupdf as fitz
    with fitz.open(path) as pdf:
        assert len(pdf) == 1
        # This authored control contains no spaces. Disable the extractor's
        # inferred spaces so tracking gaps cannot invent extra characters.
        chars = [c for b in pdf[0].get_text('rawdict', flags=fitz.TEXTFLAGS_RAWDICT | fitz.TEXT_INHIBIT_SPACES)['blocks'] for l in b.get('lines', [])
                 for s in l['spans'] for c in s['chars'] if not c['c'].isspace()]
        assert ''.join(c['c'] for c in chars) == '가나다라마바사아자차카타파하'
        return [list(c['origin']) for c in chars]


def main():
    p = argparse.ArgumentParser(); p.add_argument('--output', type=Path, required=True)
    p.add_argument('--executable', type=Path); p.add_argument('--baseline', type=Path)
    p.add_argument('--office', action='store_true'); a = p.parse_args()
    out = a.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'version': __version__, 'cases': {},
              'executable_sha256': sha(a.executable) if a.executable else None}
    def save(): (out / 'report.json').write_text(json.dumps(report, indent=2), encoding='utf-8')
    def convert(source, folder, exe, target='docx'):
        prefix = [str(exe.resolve())] if exe else [sys.executable, '-B', str(ROOT / 'run_magic_converter.py')]
        receipt = folder.with_suffix('.json')
        done = subprocess.run(prefix + ['convert', str(source), '--action', 'document:' + target, '--output', str(folder),
            '--data-dir', str(out / 'profile'), '--report', str(receipt), '--json', '--timeout', '60'],
            capture_output=True, timeout=120, env={**os.environ, 'PYTHONUTF8': '1'}, creationflags=subprocess.CREATE_NO_WINDOW)
        result = json.loads(receipt.read_text(encoding='utf-8-sig')); assert done.returncode == 0 and result['ok'], result
        return Path(result['jobs'][0]['outputs'][0])
    save()
    try:
        from magic_converter.core.documents.hwp_documents import read_docx
        from tools.validation.native_office_export import export_pdf
        for name in ('character', 'paragraph', 'defaults', 'scale', 'zero'):
            folder = out / name; folder.mkdir(); source = folder / 'source.docx'
            expected = fixture(source, name); before = sha(source)
            initial = read_docx(source).blocks[0].runs[0]
            assert (initial.spacing_pt, initial.width_percent) == expected
            middle = convert(source, folder / 'to-hwpx', a.executable, 'hwpx')
            target = convert(middle, folder / 'converted', a.executable)
            model = read_docx(target).blocks[0].runs[0]
            assert (model.spacing_pt, model.width_percent) == expected
            case = {'status': 'running', 'expected_spacing_pt': expected[0], 'expected_width_percent': expected[1],
                    'input_sha256': before}; report['cases'][name] = case; save()
            if a.office:
                for label, path in (('source', source), ('returned', target)):
                    pdf = folder / (label + '.pdf')
                    case[label + '_export'] = export_pdf(path, pdf, folder / (label + '-export'), a.executable)
                    case[label + '_origins'] = origins(pdf)
                error = max(abs(x-y) for left, right in zip(case['source_origins'], case['returned_origins']) for x, y in zip(left, right))
                case['max_raw_origin_error_pt'] = error
                assert error <= .1, error
                if a.baseline:
                    old_middle = convert(source, folder / 'baseline-to-hwpx', a.baseline, 'hwpx')
                    old = convert(old_middle, folder / 'baseline', a.baseline)
                    old_pdf = folder / 'baseline.pdf'
                    case['baseline_export'] = export_pdf(old, old_pdf, folder / 'baseline-export', a.baseline)
                    old_origins = origins(old_pdf)
                    old_error = max(abs(x-y) for left, right in zip(case['source_origins'], old_origins) for x, y in zip(left, right))
                    case['baseline_max_raw_origin_error_pt'] = old_error
                    assert old_error <= .1 if name == 'zero' else old_error > .1
            assert sha(source) == before
            case['status'] = 'passed'; save()
        report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
    save(); print(report['status']); return int(report['status'] != 'passed')


if __name__ == '__main__': raise SystemExit(main())
