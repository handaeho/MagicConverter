"""Opaque glyph intersection pixels, damaged compound-fill and deletion controls."""
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pymupdf as fitz
from PIL import Image, ImageChops, ImageStat
from pptx import Presentation
from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import opaque_overlap_pdf
from magic_converter.core.diagnostics.visual_comparison import compare, sha
from magic_converter.core.pdf.pdf_paint import PdfPaint
from magic_converter.core.pdf.pdf_pptx_glyphs import glyph_outlines
from magic_converter.core.pdf.pdf_pptx_graphics import add_path
from tools.validation.native_office_export import export_pdf

ROI = (35, 75, 240, 145)


def image(path, dpi=144, clip=ROI):
    with fitz.open(path) as pdf:
        pix = pdf[0].get_pixmap(dpi=dpi, clip=clip, colorspace=fitz.csRGB)
        return Image.frombytes('RGB', (pix.width, pix.height), pix.samples)


def mae(a, b):
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean) / 3


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--office', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'version': __version__, 'cases': {},
              'native_office': 'requested' if args.office else 'not_requested',
              'executable_sha256': sha(args.executable) if args.executable else None,
              'roi': ROI, 'dpi': [72, 96, 144, 200, 300], 'roi_gate_dpi': 144, 'roi_mae_ceiling': 2,
              'renderer': 'pinned PyMuPDF; Poppler unavailable'}

    def save():
        (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')

    save()
    try:
        cases = [('opaque', {}, 4), ('counters', {'text': 'OOOO'}, 4),
                 ('translucent', {'opacity': .5}, 4), ('separated', {'overlap': False}, 1),
                 ('clipped', {'clipped': True}, 0), ('nested-counter', {'nested_counter': True}, 1),
                 ('small-overlap', {'tracking': -12}, 4),
                 ('linear-subpoint', {'tracking': -3.5}, 4),
                 ('linear-edge-control', {'tracking': -2.5}, 1)]
        for name, options, expected in cases:
            folder = out/name; folder.mkdir()
            source = folder/'source.pdf'; opaque_overlap_pdf(source, **options)
            original = sha(source); case = {'status': 'running', 'source_sha256': original}
            report['cases'][name] = case; save()
            env = {**os.environ, 'PYTHONUTF8': '1', 'PATH': str(Path(os.environ['SystemRoot'])/'System32')}
            for key in ('PYTHONHOME', 'PYTHONPATH', 'VIRTUAL_ENV', 'QT_PLUGIN_PATH', 'QT_QPA_PLATFORM_PLUGIN_PATH'):
                env.pop(key, None)
            prefix = [str(args.executable.resolve())] if args.executable else [sys.executable, str(ROOT/'run_magic_converter.py')]
            result = subprocess.run(prefix+['convert', str(source), '--action', 'pptx_editable',
                '--output', str(folder/'output'), '--report', str(folder/'job.json'),
                '--data-dir', str(folder/'profile'), '--timeout', '60', '--json'],
                env=env, capture_output=True, timeout=120, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            (folder/'conversion.log').write_bytes(result.stdout+result.stderr)
            job = json.loads((folder/'job.json').read_text(encoding='utf-8-sig'))
            if result.returncode or not job['ok']:
                raise ValueError(str(job))
            target = Path(job['jobs'][0]['outputs'][0]); deck = Presentation(target)
            shapes = deck.slides[0].shapes
            groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
            texts = [s.text for s in shapes if s.has_text_frame]
            children = [len(g.shapes) for g in groups]
            if texts != ['Independent title'] or children != ([expected] if expected else []):
                raise ValueError('Independent text or glyph separation changed')
            if sum(s.shape_type == 13 for s in shapes) != int(not expected):
                raise ValueError('Complete clipped replay was lost or unrequested raster used')
            if any(not s._element.xpath('./p:spPr/a:custGeom') for g in groups for s in g.shapes):
                raise ValueError('Recovered glyph is not an editable curve')
            ids = [int(n.get('id')) for n in deck.slides[0]._element.xpath('.//p:cNvPr')]
            if len(ids) != len(set(ids)):
                raise ValueError('Duplicate slide shape ids')
            case.update(children=children, editable_title=True, unique_shape_ids=True)
            if args.office:
                native = folder/'native.pdf'
                case['native_export'] = export_pdf(target, native, folder/'native-export', args.executable)
                case['roi_mae'] = {str(dpi): mae(image(source, dpi), image(native, dpi)) for dpi in report['dpi']}
                case['visual'] = compare(source, native, dpi=144, artifacts=folder/'visual')
                if expected == 4:
                    if case['roi_mae'][str(report['roi_gate_dpi'])] >= report['roi_mae_ceiling']:
                        raise ValueError('Glyph intersection pixels exceeded fixed ROI ceiling')
                    case['visual_gate'] = '144dpi intersection ROI MAE < 2'
                else:
                    if case['visual']['pages'][0]['mae'] >= 1:
                        raise ValueError('Unchanged separated/clipped control exceeded page MAE ceiling')
                    case['visual_gate'] = '144dpi unchanged control page MAE < 1; ROI measured separately'
                if name in ('opaque', 'counters', 'small-overlap', 'linear-subpoint'):
                    # Recreate the prior valid-but-wrong compound shape. For
                    # small kerning the first child is the unchanged compound;
                    # the other children fill only its cancelled intersections.
                    with fitz.open(source) as pdf:
                        paint = PdfPaint(pdf[0]); sequences = list(range(1, len(paint.log)))
                        bounds = fitz.Rect()
                        for n in sequences:
                            bounds |= paint.visible_bounds(n)
                        drawings = glyph_outlines(paint, sequences, bounds,
                            {t['seqno']: t for t in pdf[0].get_texttrace()})
                        if not drawings or len(drawings) != 4:
                            raise ValueError('Missing independent original glyph outlines')
                    merged = copy.deepcopy(drawings[0]); merged['items'] = []
                    merged['rect'] = fitz.Rect()
                    for drawing in (drawings[:1] if name in ('small-overlap', 'linear-subpoint') else drawings):
                        merged['items'].extend(drawing['items']); merged['rect'] |= drawing['rect']
                    wrong_deck = Presentation(target); wrong_shapes = wrong_deck.slides[0].shapes
                    for group in list(wrong_shapes):
                        if group.name.startswith('PDF glyph outline region '):
                            group._element.getparent().remove(group._element)
                    add_path(wrong_deck.slides[0], merged, 1, 0, 0)
                    wrong = folder/'compound-negative.pptx'; wrong_deck.save(wrong)
                    case['negative_export'] = export_pdf(wrong, folder/'compound-negative.pdf', folder/'negative-export', args.executable)
                    case['compound_negative_roi_mae'] = mae(image(source), image(folder/'compound-negative.pdf'))
                    if name != 'linear-subpoint' and case['compound_negative_roi_mae'] < report['roi_mae_ceiling']:
                        raise ValueError('Compound-fill negative escaped the same pixel gate')
                    case['compound_negative_roi_mae_by_dpi'] = {
                        str(dpi): mae(image(source, dpi), image(folder/'compound-negative.pdf', dpi))
                        for dpi in report['dpi']}
                    required_gain = .005 if name == 'linear-subpoint' else 2
                    case['negative_minimum_gain_per_dpi'] = required_gain
                    if any(case['compound_negative_roi_mae_by_dpi'][dpi] <= value + required_gain
                           for dpi, value in case['roi_mae'].items()):
                        raise ValueError('Compound-fill defect was not reduced at every sampled DPI')
                    # Removing the recovered group must erase only its ink.
                    group = groups[0]; group._element.getparent().remove(group._element)
                    deleted = folder/'deleted.pptx'; deck.save(deleted)
                    case['deletion_export'] = export_pdf(deleted, folder/'deleted.pdf', folder/'deletion-export', args.executable)
                    if min(image(folder/'deleted.pdf').tobytes()) < 250:
                        raise ValueError('Deleted glyphs left a ghost')
                    if image(native, clip=(205, 10, 395, 45)).tobytes() != image(folder/'deleted.pdf', clip=(205, 10, 395, 45)).tobytes():
                        raise ValueError('Deleting glyphs changed the independent title')
                    case.update(compound_negative='rejected', deletion='passed', title_after_deletion='unchanged')
            if sha(source) != original:
                raise ValueError('Original changed')
            case.update(status='passed', original_unchanged=True); save()
        report.update(status='passed', native_office='passed' if args.office else 'not_requested')
    except Exception as exc:
        report.update(status='failed', error=type(exc).__name__+': '+str(exc))
    save(); print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
