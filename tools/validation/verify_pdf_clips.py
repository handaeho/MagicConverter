"""Source/frozen clipping/Type3/mask preservation and native deletion controls."""
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
from PIL import Image, ImageChops, ImageStat
from pptx import Presentation
from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import clipped_sequence_pdf
from magic_converter.core.diagnostics.visual_comparison import compare, sha
from tools.validation.native_office_export import export_pdf


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    parser.add_argument('--office', action='store_true')
    args = parser.parse_args(); root = args.output.resolve(); root.mkdir(parents=True, exist_ok=False)
    report = {'status':'running', 'version':__version__, 'cases':[],
              'executable_sha256':sha(args.executable) if args.executable else None,
              'native_office':'pending' if args.office else 'skipped'}
    def save(): (root/'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    save()
    try:
        prefix = [str(args.executable.resolve())] if args.executable else [sys.executable, str(ROOT/'run_magic_converter.py')]
        for label, clip in [('outside','20 20 40 20'), ('empty','20 20 0 0'),
                            ('partial','20 110 12 30'), ('inside','0 0 360 240')]:
            for type3 in (False, True):
                for mask in (False, True):
                    name = f'{label}-'+('type3' if type3 else 'regular')+('-mask' if mask else '')
                    source = root/(name+'.pdf'); clipped_sequence_pdf(source, clip, type3=type3, mask=mask)
                    original = sha(source); job_path=root/(name+'-job.json')
                    done = subprocess.run(prefix+['convert',str(source),'--action','pptx_editable',
                        '--output',str(root/name),'--report',str(job_path),'--data-dir',str(root/'profile'),
                        '--timeout','60','--json'],capture_output=True,timeout=120,
                        env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
                    job=json.loads(job_path.read_text(encoding='utf-8-sig'))
                    if done.returncode or not job['ok']: raise ValueError(str(job))
                    target=Path(job['jobs'][0]['outputs'][0]); deck=Presentation(target); shapes=deck.slides[0].shapes
                    texts=[s.text for s in shapes if s.has_text_frame and s.text]
                    hidden=label in ('outside','empty')
                    case={'case':name,'source_sha256':original,'text':texts,
                          'pictures':sum(s.shape_type==13 for s in shapes),
                          'whole_page_fallback':any(s.name=='PDF preserved page (paint sequence mismatch)' for s in shapes)}
                    report['cases'].append(case); save()
                    if case['whole_page_fallback'] or 'Independent title' not in texts:
                        raise ValueError('Independent text was flattened')
                    uniform_vector = label=='inside' and type3 and mask
                    expected_pictures = int(not hidden and (mask or label=='partial') and not uniform_vector)
                    if case['pictures'] != expected_pictures: raise ValueError('Clipped or masked paint was lost/duplicated')
                    if label=='inside' and type3:
                        if not any(s.name.startswith('PDF glyph outline region ') for s in shapes):
                            raise ValueError('Simple Type3 polygon was not restored as an editable curve')
                        if uniform_vector:
                            group=next(s for s in shapes if s.name.startswith('PDF glyph outline region '))
                            case['vector_fill_alpha']=[int(n.get('val'))for n in group._element.xpath('.//a:solidFill/a:srgbClr/a:alpha')]
                            if case['vector_fill_alpha'] != [50000]:raise ValueError('Uniform Type3 mask alpha changed')
                    if not hidden and mask and not uniform_vector:
                        picture=next(s for s in shapes if s.shape_type==13)
                        with Image.open(io.BytesIO(picture.image.blob)) as image:
                            case['maximum_alpha']=image.getchannel('A').getextrema()[1]
                        if not 120 <= case['maximum_alpha'] <= 132: raise ValueError('Soft mask lost')
                    # Compare actual Office for hidden and partial paints of both
                    # font kinds. Unmasked controls remain structural comparisons.
                    if args.office and ((mask and label in ('outside','partial')) or (type3 and label=='inside')):
                        native=root/(name+'-native.pdf')
                        case['native_export']=export_pdf(target,native,root/(name+'-export'),args.executable)
                        case['visual']=compare(source,native,artifacts=root/(name+'-visual'))
                        if case['visual']['pages'][0]['mae'] >= 1: raise ValueError('Native clip/mask pixels changed')
                        if not hidden:
                            for shape in list(shapes):
                                if shape.shape_type==13 or shape.name.startswith('PDF glyph outline region '):shape._element.getparent().remove(shape._element)
                            edited=root/(name+'-deleted.pptx');deck.save(edited)
                            deleted=root/(name+'-deleted.pdf')
                            case['deletion_export']=export_pdf(edited,deleted,root/(name+'-deleted-export'),args.executable)
                            with fitz.open(deleted) as pdf:
                                if 'Independent title' not in pdf[0].get_text():raise ValueError('Independent title disappeared')
                                if min(pdf[0].get_pixmap(clip=fitz.Rect(0,60,180,180)).samples)<250:
                                    raise ValueError('Deleted clipped picture left a ghost')
                            if mask:
                                negative=root/(name+'-no-mask.pdf');clipped_sequence_pdf(negative,clip,type3=type3,mask=False)
                                case['lost_mask_negative']=compare(source,negative)['pages'][0]['mae']
                                if case['lost_mask_negative'] <= .05:raise ValueError('Mask-loss negative was not distinguished')
                        case['native_office']='passed'
                    if sha(source)!=original:raise ValueError('Original input changed')
                    case['status']='passed';save()
        report['status']='passed'
        if args.office:report['native_office']='passed'
    except Exception as error: report.update(status='failed', error=f'{type(error).__name__}: {error}')
    save();print(json.dumps(report, ensure_ascii=True, indent=2))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
