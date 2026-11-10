"""Source/frozen structure and actual Office pixels, clipping and deletion controls."""
import argparse
import hashlib
import io
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
from pptx.util import Pt
from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import overlapping_glyph_pdf, tight_table_pdf
from magic_converter.core.diagnostics.visual_comparison import compare, sha
from tools.validation.native_office_export import export_pdf


def roi(path):
    with fitz.open(path) as doc:
        pix = doc[0].get_pixmap(dpi=144, clip=(38, 44, 88, 93), colorspace=fitz.csRGB)
        return Image.frombytes('RGB', (pix.width, pix.height), pix.samples)


def mae(a, b):
    return sum(ImageStat.Stat(ImageChops.difference(a, b)).mean)/3


def fill_rule_consistent(shape):
    """Reject stroke overlaps that Office turns into small parity-rule holes."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QPainterPath
    paths=shape._element.xpath('./p:spPr/a:custGeom/a:pathLst/a:path')
    if len(paths)!=1:return False
    path=QPainterPath()
    for node in paths[0]:
        command=node.tag.rsplit('}',1)[-1]
        points=[(float(p.get('x')),float(p.get('y')))for p in node]
        if command=='moveTo':path.moveTo(*points[0])
        elif command=='lnTo':path.lineTo(*points[0])
        elif command=='cubicBezTo':path.cubicTo(*(v for point in points for v in point))
        elif command=='close':path.closeSubpath()
        else:return False
    rect=path.boundingRect()
    if rect.isEmpty():return False
    winding=QPainterPath(path);winding.setFillRule(Qt.WindingFill)
    parity=QPainterPath(path);parity.setFillRule(Qt.OddEvenFill)
    for x in range(128):
        for y in range(128):
            point=QPointF(rect.left()+rect.width()*(x+.5)/128,rect.top()+rect.height()*(y+.5)/128)
            if winding.contains(point)!=parity.contains(point):return False
    return True


def stroke_fixture(path):
    with fitz.open() as doc:
        page = doc.new_page(width=220, height=160)
        page.draw_rect((30, 40, 105, 105), color=None, fill=(.8, .9, 1))
        page.insert_text((20, 25), 'Independent title', fontsize=12)
        before = page.read_contents()
        page.insert_text((40, 90), 'WW', fontsize=60, render_mode=1, stroke_opacity=.5)
        content = before + page.read_contents()[len(before):].replace(b'BT', b'BT -50 Tc')
        stream = doc.get_new_xref(); doc.update_object(stream, '<<>>')
        doc.update_stream(stream, content); page.set_contents(stream); doc.save(path)


def baselines(source, candidate):
    def positions(path):
        with fitz.open(path) as doc:
            return {' '.join(s['text'].split()): s['origin'] for b in doc[0].get_text('dict')['blocks']
                    for line in b.get('lines', []) for s in line['spans']}
    a, b = positions(source), positions(candidate)
    if set(a) != set(b):
        raise ValueError('Cell/outside characters changed or wrapped')
    return max(abs(a[text][1]-b[text][1]) for text in a)


def main():
    parser = argparse.ArgumentParser(); parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path); parser.add_argument('--office', action='store_true')
    args = parser.parse_args(); out = args.output.resolve(); out.mkdir(parents=True, exist_ok=False)
    report = {'status': 'running', 'version': __version__, 'native': 'requested' if args.office else 'not_requested',
              'executable_sha256': sha(args.executable) if args.executable else None, 'cases': {}}
    def save(): (out/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    try:
        decks = {}
        for name, fixture in [('overlap', overlapping_glyph_pdf), ('overlap-stroke', stroke_fixture), ('tight', tight_table_pdf)]:
            folder = out/name; folder.mkdir(); source = folder/'source.pdf'; fixture(source); before = sha(source)
            prefix = [str(args.executable.resolve())] if args.executable else [sys.executable, '-B', str(ROOT/'run_magic_converter.py')]
            jobfile = folder/'job.json'
            done = subprocess.run([*prefix, 'convert', str(source), '--action', 'pptx_editable', '--output', str(folder/'output'),
                                   '--report', str(jobfile), '--data-dir', str(folder/'profile'), '--timeout', '60', '--json'],
                                  capture_output=True, timeout=120, env={**os.environ, 'PYTHONUTF8': '1'},
                                  creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
            job = json.loads(jobfile.read_text(encoding='utf-8-sig'))
            if done.returncode or not job['ok']: raise ValueError(str(job))
            target = Path(job['jobs'][0]['outputs'][0]); deck = Presentation(target); shapes = deck.slides[0].shapes
            with fitz.open(source) as fixture_pdf:
                pixel_sha = hashlib.sha256(fixture_pdf[0].get_pixmap(dpi=144, alpha=False).samples).hexdigest()
            case = {'source_sha256': before, 'source_pixels_sha256_144dpi': pixel_sha, 'structure': 'passed'}; report['cases'][name] = case
            if name.startswith('overlap'):
                groups = [s for s in shapes if s.name.startswith('PDF glyph outline region ')]
                if len(groups) != 1 or len(groups[0].shapes) != 2 or any(s.shape_type == 13 for s in shapes):
                    raise ValueError('Overlapping alpha ink must remain two curves without raster duplicates')
                if [int(a.get('val')) for s in groups[0].shapes for a in s._element.xpath('.//a:alpha')] != [50000, 50000]:
                    raise ValueError('Overlap alpha lost')
                if name == 'overlap-stroke':
                    if any(not s._element.xpath('./p:spPr/a:ln/a:noFill') for s in groups[0].shapes):
                        raise ValueError('Stroke silhouette acquired a duplicate Office line')
                    if not all(fill_rule_consistent(s)for s in groups[0].shapes):
                        raise ValueError('Stroke overlap leaves parity-rule holes at intersections')
                    case['stroke_boundary'] = 'separate_filled_silhouettes'
                    case['intersection_fill_rule_control'] = 'passed'
                case['outline_groups'], case['outline_shapes'] = len(groups), len(groups[0].shapes)
            else:
                tables = [s.table for s in shapes if s.has_table]
                regions = [s for s in shapes if s.name.startswith('PDF cell R')]
                if len(tables) != 1 or len(regions) != 6 or tables[0].cell(0, 0).text or tables[0].cell(1, 0).text:
                    raise ValueError('Tight cell duplicated, not positioned, or table lost')
                if 'Normal line 1 WWW iii' not in tables[0].cell(0, 1).text.replace('\xa0', ' '):
                    raise ValueError('Normal cell lost native editing')
                case['positioned_cell_textboxes'] = len(regions)
            if args.office:
                native = folder/'native.pdf'; case['export'] = export_pdf(target, native, folder/'export', args.executable)
                case['native_pdf_sha256'] = sha(native)
                with fitz.open(native) as native_pdf:
                    case['native_pixels_sha256_144dpi'] = hashlib.sha256(native_pdf[0].get_pixmap(dpi=144, alpha=False).samples).hexdigest()
                case['visual'] = compare(source, native, dpi=144, artifacts=folder/'visual')
                if name.startswith('overlap'):
                    case['roi_mae'] = mae(roi(source), roi(native))
                    if name == 'overlap-stroke':
                        case['pixel_gate'] = 'roi_mae_under_6_and_same_input_release_improvement_at_5_resolutions'
                        if case['roi_mae'] >= 6: raise ValueError('Stroke silhouette exceeded ROI ceiling')
                        if case['visual']['pages'][0]['mae'] >= 1.5: raise ValueError('Stroke exceeded existing whole-page ceiling')
                    elif case['roi_mae'] >= 2:
                        raise ValueError('Overlapping alpha fill pixels changed')
                    for s in groups[0].shapes:
                        for a in s._element.xpath('.//a:alpha'): a.set('val', '100000')
                    case['negative_control'] = 'remove curve alpha'
                    wrong = folder/'wrong-alpha.pptx'; deck.save(wrong)
                    case['wrong_alpha_export'] = export_pdf(wrong, folder/'wrong-alpha.pdf', folder/'wrong-export', args.executable)
                    case['wrong_alpha_roi_mae'] = mae(roi(source), roi(folder/'wrong-alpha.pdf'))
                    case['wrong_alpha_delta_from_native_roi_mae'] = mae(roi(native), roi(folder/'wrong-alpha.pdf'))
                    if case['wrong_alpha_delta_from_native_roi_mae'] < 5:
                        raise ValueError('Wrong alpha escaped same fill ROI gate')
                    for region in groups:
                        region._element.getparent().remove(region._element)
                    deleted = folder/'deleted.pptx'; deck.save(deleted)
                    case['deleted_export'] = export_pdf(deleted, folder/'deleted.pdf', folder/'deleted-export', args.executable)
                    with fitz.open(folder/'deleted.pdf') as pdf:
                        pix = pdf[0].get_pixmap(clip=(38,44,88,93), colorspace=fitz.csRGB)
                        if any(abs(v-(204,230,255)[i%3]) > 3 for i,v in enumerate(pix.samples)):
                            raise ValueError('Deleted overlap left ink or changed backdrop')
                        if 'Independent title' not in pdf[0].get_text(): raise ValueError('Independent title lost')
                    case['deletion'] = 'passed'
                else:
                    case['max_baseline_error_pt'] = baselines(source, native)
                    if case['max_baseline_error_pt'] >= .6: raise ValueError('Tight or normal baseline moved')
                    for s in regions: s.width = Pt(10); s.text_frame.word_wrap = True
                    clipped = folder/'clipped.pptx'; deck.save(clipped)
                    case['clipped_export'] = export_pdf(clipped, folder/'clipped.pdf', folder/'clipped-export', args.executable)
                    try:
                        error = baselines(source, folder/'clipped.pdf')
                    except ValueError:
                        error = None
                    if error is not None and error < .6: raise ValueError('Clipping negative escaped baseline/content gate')
                    case['clipping_negative'] = 'rejected'
            if sha(source) != before: raise ValueError('Fixture input changed')
            save()
        report['status'] = 'passed'
    except Exception as exc:
        report.update(status='failed', error=type(exc).__name__ + ': ' + str(exc))
    save(); print(json.dumps({'status': report['status'], 'error': report.get('error')}), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__': raise SystemExit(main())
