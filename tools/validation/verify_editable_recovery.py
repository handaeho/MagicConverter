"""Compare bounded tracking and partial glyph clips through native Office."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import traceback

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pymupdf as fitz
from docx import Document
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from PIL import Image, ImageChops, ImageFilter, ImageStat
from pptx import Presentation

from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import clipped_sequence_pdf, type3_curve_pdf
from magic_converter.core.diagnostics.visual_comparison import compare, sha
from magic_converter.core.documents.character_spacing import metric_characters
from magic_converter.core.documents.hwp_documents import read_docx, read_hwpx
from tools.validation.native_office_export import export_pdf

DPI = (72, 96, 144, 200, 300)
WORD_CASES = {
    'latin': ('GulimChe', 'ABZ iW9-ABZ', .6, .1),
    'mixed-negative': ('굴림체', '가A나 9다W-라마z', -1.2, .1),
    'mixed-positive': ('DotumChe', 'AB가cd9나-E라 W', .6, .1),
    # Non-integral HWPX percentages incur model rounding plus Word's native
    # PDF coordinate quantization. Score this separately from exact metrics.
    'mixed-quantized': ('돋움체', '가A나9다W-마zB바가A나9다W-마zB바', .65, .25),
    'proportional': ('Arial', 'AV office WIDTH', .6, None),
}
PDF_CASES = {
    'triangle': ('20 114 12 24', True, False, True),
    'latin-I': ('22 111 12 27', False, False, True),
    'curved': ('20 114 12 24', False, False, False),
    'soft-mask': ('20 114 12 24', True, 'gradient', False),
    'slanted-clip': ('20 114 12 24', True, False, False),
}
WORD_FOLLOWUP_CASES = {
    'tahoma-positive': ('Tahoma', 'HELIX 20 RUN 43', .6, .25),
    'tahoma-negative': ('Tahoma', 'SUM 97 MIX 20', -.6, .25),
    'tahoma-positioned': ('Tahoma', 'HIM 20 WN', .6, None),
    'tahoma-kerning': ('Tahoma', 'AV LO 93', .6, None),
}
PDF_FOLLOWUP_CASES = {
    'multi-latin': ('22 111 62 27', False, False, True),
    'multi-overlap': ('22 111 62 27', False, False, False),
    'multi-near': ('22 111 62 27', False, False, False),
    'multi-curved': ('22 111 62 27', False, False, False),
}
_MULTI_PROGRAMS = {
    'multi-latin': b'/helv 40 Tf (I I I) Tj',
    'multi-overlap': b'/helv 40 Tf [(I) 250 (I)] TJ',
    'multi-near': b'/helv 40 Tf [(I) 178 (I)] TJ',
    'multi-curved': b'/helv 40 Tf (I O I) Tj',
}
WORD_CURVE_RELEASE_CASES = {
    'tahoma-lower-positive': ('Tahoma', 'number 20 RUN', .6, .25),
    'tahoma-lower-negative': ('Tahoma', 'house 97 HI', -.6, .25),
    'tahoma-lower-kern': ('Tahoma', 'number Re 20', .6, None),
    'tahoma-lower-ligature': ('Tahoma', 'office 20', .6, None),
}
PDF_CURVE_RELEASE_CASES = {
    'one-edge-top': ('12 100 38 36', True, False, True),
    'one-edge-left': ('26 100 30 60', True, False, True),
    'one-edge-masked': ('12 100 38 36', True, True, False),
    'partial': ('20 110 12 30', True, False, False),
}
PDF_CORNER_CASES = {
    'two-edge-corner': ('26 100 30 36', True, False, True),
    'two-edge-masked': ('26 100 30 36', True, True, False),
}


def convert(source, folder, executable, action):
    prefix = [str(executable.resolve())] if executable else [sys.executable, '-B', str(ROOT / 'run_magic_converter.py')]
    receipt = folder.with_suffix('.json')
    done = subprocess.run(prefix + ['convert', str(source), '--action', action, '--output', str(folder),
        '--data-dir', str(folder.parent / 'profile'), '--report', str(receipt), '--json', '--timeout', '60'],
        capture_output=True, timeout=120, env={**os.environ, 'PYTHONUTF8': '1'},
        creationflags=subprocess.CREATE_NO_WINDOW)
    (folder.with_suffix('.log')).write_bytes(done.stdout + done.stderr)
    result = json.loads(receipt.read_text(encoding='utf-8-sig'))
    assert done.returncode == 0 and result['ok'], result
    job = result['jobs'][0]
    return Path(job['outputs'][0]), job.get('warnings', [])


def word_origins(path, text):
    with fitz.open(path) as pdf:
        assert len(pdf) == 1
        flags = fitz.TEXTFLAGS_RAWDICT | fitz.TEXT_INHIBIT_SPACES
        spans = [span for block in pdf[0].get_text('rawdict', flags=flags)['blocks']
                 for line in block.get('lines', []) for span in line['spans']]
        visible = [(c, span['font']) for span in spans for c in span['chars'] if not c['c'].isspace()]
        chars = [c for c, _ in visible]
        assert ''.join(c['c'] for c in chars) == text.replace(' ', '')
        return [c['origin'] for c in chars], sorted({font for _, font in visible})


def word_case(folder, name, specification, executable, baseline, native, case, *, regression=False):
    font, text, spacing, origin_limit = specification
    supported = origin_limit is not None
    doc = Document()
    run = doc.add_paragraph().add_run(text)
    run.font.name, run.font.size = font, Pt(12)
    run._r.get_or_add_rPr().rFonts.set(qn('w:eastAsia'), font)
    node = OxmlElement('w:spacing')
    node.set(qn('w:val'), str(round(spacing * 20)))
    run._r.get_or_add_rPr().append(node)
    if name in {'tahoma-positioned', 'tahoma-kerning', 'tahoma-lower-kern'}:
        tag, value = ('position', '4') if name == 'tahoma-positioned' else ('kern', '0')
        node = OxmlElement('w:' + tag)
        node.set(qn('w:val'), value)
        run._r.get_or_add_rPr().append(node)
    source = folder / 'source.docx'
    doc.save(source)
    case.update(text=text, font=font, spacing_pt=spacing, native_origin_limit_pt=origin_limit,
                input_sha256=sha(source))
    if font == 'Tahoma':
        from magic_converter.core.pdf.pdf_fonts import resolve_font
        face = resolve_font(font)
        case['font_file_sha256'] = sha(face.path) if face is not None else None
    middle, warnings = convert(source, folder / 'to-hwpx', executable, 'document:hwpx')
    target, returned_warnings = convert(middle, folder / 'returned', executable, 'document:docx')
    hwpx, returned = read_hwpx(middle), read_docx(target)
    assert hwpx.text() == returned.text() == text
    runs = hwpx.blocks[0].runs
    case.update(hwpx_runs=[(r.text, r.spacing_percent) for r in runs],
                returned_runs=[(r.text, r.spacing_pt) for r in returned.blocks[0].runs],
                to_hwpx_warnings=warnings, returned_warnings=returned_warnings)
    if supported:
        for model in (hwpx, returned):
            drift = 0
            for r in model.blocks[0].runs:
                for _, unit, value, width in metric_characters(r):
                    assert unit == 'pt' and width == 100
                    drift += value - spacing
                    assert abs(drift) <= .1, drift
    else:
        assert all(not r.spacing_percent for r in runs)
        assert any('자간' in w for w in warnings)
    if native:
        previous = None
        if baseline:
            old_middle, _ = convert(source, folder / 'baseline-hwpx', baseline, 'document:hwpx')
            previous, _ = convert(old_middle, folder / 'baseline', baseline, 'document:docx')
        for label, path, exe in (('source', source, executable), ('returned', target, executable),
                                 ('baseline', previous, baseline)):
            if path is None:
                continue
            pdf = folder / (label + '.pdf')
            case[label + '_export'] = export_pdf(path, pdf, folder / (label + '-export'), exe)
            origins, fonts = word_origins(pdf, text)
            case[label + '_origins'] = origins
            case[label + '_fonts'] = fonts
            if label != 'source':
                assert fonts == case['source_fonts']
                error = max(abs(x-y) for a, b in zip(case['source_origins'], origins) for x, y in zip(a, b))
                case[label + '_max_error_pt'] = error
        if supported:
            assert case['returned_max_error_pt'] <= origin_limit, case['returned_max_error_pt']
            if baseline and not regression:
                assert case['baseline_max_error_pt'] > max(.1, case['returned_max_error_pt'])
        if baseline and (regression or not supported):
            assert case['returned_origins'] == case['baseline_origins']
    assert sha(source) == case['input_sha256']


def pdf_fixture(source, name, clip, type3, mask):
    if name in PDF_CURVE_RELEASE_CASES or name in PDF_CORNER_CASES:
        type3_curve_pdf(source, name)
        return
    clipped_sequence_pdf(source, clip, type3=type3, mask=mask, text_program=_MULTI_PROGRAMS.get(name))
    if name in {'latin-I', 'slanted-clip'}:
        with fitz.open(source) as pdf:
            stream = pdf[0].get_contents()[0]
            original = pdf.xref_stream(stream)
            if name == 'latin-I':
                updated = original.replace(b'/helv 20 Tf (Clipped text)', b'/helv 40 Tf (I)')
            else:
                updated = original.replace(b'20 114 12 24 re', b'20 114 m 32 114 l 32 138 l 20.004 138 l h')
            assert updated != original
            pdf.update_stream(stream, updated)
            pdf.saveIncr()


def crop_metrics(reference, candidate, bounds, dpi, *, curve_edges=False):
    images = []
    for path in (reference, candidate):
        with fitz.open(path) as pdf:
            p = pdf[0].get_pixmap(clip=bounds, dpi=dpi, alpha=False)
            images.append(Image.frombytes('RGB', (p.width, p.height), p.samples))
    with images[0] as first, images[1] as second, ImageChops.difference(first, second) as diff:
        mae = sum(ImageStat.Stat(diff).mean) / 3
        if not curve_edges:
            return mae
        # PDF Type3 glyph clipping and native Office vector exports quantize
        # antialiased edges differently. Compare dark-ink support in both
        # directions and require every disagreement to stay within one pixel.
        a, b = (image.convert('L').point(lambda value: 255 if value < 128 else 0) for image in (first, second))
        outside = (ImageChops.subtract(a, b.filter(ImageFilter.MaxFilter(3))).histogram()[255]
                   + ImageChops.subtract(b, a.filter(ImageFilter.MaxFilter(3))).histogram()[255])
        return {'mae': mae, 'ink_pixels_outside_one_pixel': outside}


def pdf_case(folder, name, specification, executable, baseline, native, case, *, regression=False):
    clip, type3, mask, editable = specification
    source = folder / 'source.pdf'
    pdf_fixture(source, name, clip, type3, mask)
    case['input_sha256'] = sha(source)
    target, _ = convert(source, folder / 'converted', executable, 'pptx_editable')
    deck = Presentation(target)
    shapes = deck.slides[0].shapes
    groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
    pictures = sum(s.shape_type == 13 for s in shapes)
    case.update(pictures=pictures, groups=len(groups))
    assert (pictures == 0 and len(groups) == 1) if editable else (pictures == 1 and not groups)
    assert 'Independent title' in [s.text for s in shapes if s.has_text_frame]
    if editable:
        glyph = groups[0].shapes[0]
        assert len(groups[0].shapes) == 1
        if name in PDF_CURVE_RELEASE_CASES or name in PDF_CORNER_CASES:
            case['editable_cubics'] = len(glyph._element.xpath('.//a:cubicBezTo'))
            assert case['editable_cubics'] >= 2
        else:
            assert glyph._element.xpath('.//a:lnTo')
        if name == 'multi-latin':
            case['editable_subpaths'] = len(glyph._element.xpath('.//a:moveTo'))
            assert case['editable_subpaths'] == 3
        x, y, w, h = map(float, clip.split())
        extent = fitz.Rect(glyph.left.pt, glyph.top.pt, (glyph.left + glyph.width) / 12700,
                           (glyph.top + glyph.height) / 12700)
        allowed = fitz.Rect(x, 240-y-h, x+w, 240-y) + (-.001, -.001, .001, .001)
        assert allowed.contains(extent), (extent, allowed)
        case['shape_bounds_pt'] = list(extent)
    if native:
        candidate = folder / 'native.pdf'
        case['native_export'] = export_pdf(target, candidate, folder / 'native-export', executable)
        case['page_dpi'] = {str(dpi): compare(source, candidate, dpi=dpi)['pages'][0] for dpi in DPI}
        assert all(item.get('mae', 255) < 1 for item in case['page_dpi'].values())
        curved = (name in PDF_CURVE_RELEASE_CASES or name in PDF_CORNER_CASES) and editable
        area = fitz.Rect(12, 86, 50, 136) if curved else fitz.Rect(12, 92, 94 if name in PDF_FOLLOWUP_CASES else 46, 136)
        case['region_mae'] = {str(dpi): crop_metrics(source, candidate, area, dpi) for dpi in DPI}
        if curved:
            case['edge_check'] = {str(dpi): crop_metrics(source, candidate, area, dpi, curve_edges=True)
                                  for dpi in DPI}
            assert all(not item['ink_pixels_outside_one_pixel'] for item in case['edge_check'].values())
        elif editable:
            assert max(case['region_mae'].values()) < 2, case['region_mae']
        if baseline:
            old, _ = convert(source, folder / 'baseline', baseline, 'pptx_editable')
            old_pdf = folder / 'baseline.pdf'
            old_shapes = Presentation(old).slides[0].shapes
            assert sum(s.shape_type == 13 for s in old_shapes) == int(not (regression and editable))
            case['baseline_export'] = export_pdf(old, old_pdf, folder / 'baseline-export', baseline)
            case['baseline_region_mae'] = {str(dpi): crop_metrics(source, old_pdf, area, dpi) for dpi in DPI}
            if regression or not editable:
                assert all(compare(old_pdf, candidate, dpi=dpi)['pages'][0]['mae'] == 0 for dpi in DPI)
        if name == 'two-edge-corner':
            group, = groups
            group.left += 80 * 12700
            moved, moved_pdf = folder / 'moved.pptx', folder / 'moved.pdf'
            deck.save(moved)
            case['move_export'] = export_pdf(moved, moved_pdf, folder / 'move-export', executable)
            with fitz.open(moved_pdf) as pdf:
                page = pdf[0]
                assert 'Independent title' in page.get_text()
                assert min(page.get_pixmap(clip=fitz.Rect(12, 86, 50, 136)).samples) >= 250
                assert min(page.get_pixmap(clip=fitz.Rect(92, 86, 130, 136)).samples) < 128
            group.left -= 80 * 12700
        for shape in list(shapes):
            if shape.shape_type == 13 or shape in groups:
                shape._element.getparent().remove(shape._element)
        deleted, deleted_pdf = folder / 'deleted.pptx', folder / 'deleted.pdf'
        deck.save(deleted)
        case['deletion_export'] = export_pdf(deleted, deleted_pdf, folder / 'deletion-export', executable)
        with fitz.open(deleted_pdf) as pdf:
            assert 'Independent title' in pdf[0].get_text()
            assert min(pdf[0].get_pixmap(clip=fitz.Rect(0, 60, 190, 180)).samples) >= 250
    assert sha(source) == case['input_sha256']


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--baseline', type=Path)
    parser.add_argument('--office', action='store_true')
    parser.add_argument('--kind', choices=('word', 'pdf', 'both'), default='both')
    parser.add_argument('--suite', choices=('v2222', 'v2223', 'v2224', 'v2228'), default='v2222')
    parser.add_argument('--regression', action='store_true', help='Compare against a baseline supporting the same cases')
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'version': __version__, 'office': args.office, 'suite': args.suite,
              'regression': args.regression,
              'executable_sha256': sha(args.executable) if args.executable else None,
              'baseline_sha256': sha(args.baseline) if args.baseline else None, 'cases': {}}

    def save():
        (out / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    failures = []
    word, pdf = {'v2222': (WORD_CASES, PDF_CASES), 'v2223': (WORD_FOLLOWUP_CASES, PDF_FOLLOWUP_CASES),
                 'v2224': (WORD_CURVE_RELEASE_CASES, PDF_CURVE_RELEASE_CASES),
                 'v2228': ({}, PDF_CORNER_CASES)}[args.suite]
    for kind, cases in (('word', word), ('pdf', pdf)):
        if args.kind not in (kind, 'both'):
            continue
        for name, specification in cases.items():
            folder = out / name
            folder.mkdir()
            case = report['cases'][name] = {'kind': kind, 'status': 'running'}
            try:
                if kind == 'word':
                    word_case(folder, name, specification, args.executable, args.baseline, args.office, case,
                              regression=args.regression)
                else:
                    pdf_case(folder, name, specification, args.executable, args.baseline, args.office, case,
                             regression=args.regression)
                case['status'] = 'passed'
            except Exception as error:
                case.update(status='failed', error=f'{type(error).__name__}: {error}',
                            traceback=traceback.format_exc())
                failures.append(name)
            save()
    report.update(status='failed' if failures else 'passed', failures=failures)
    save()
    print(report['status'], *failures, flush=True)
    return int(bool(failures))


if __name__ == '__main__':
    raise SystemExit(main())
