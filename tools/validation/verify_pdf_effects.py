"""Source/frozen PDF blend, independent text and real Office deletion controls."""
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
from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import effect_pdf, isolated_effect_pdf
from magic_converter.core.diagnostics.visual_comparison import compare, sha
from tools.validation.native_office_export import export_pdf


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path)
    parser.add_argument('--office',action='store_true')
    args=parser.parse_args()
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'cases':[],
            'executable_sha256':sha(args.executable) if args.executable else None,
            'native_office':'pending' if args.office else 'skipped'}
    def save(): (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        env=os.environ.copy();env['PYTHONUTF8']='1'
        env['PATH']=str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32')
        for key in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH'):
            env.pop(key,None)
        prefix=[str(args.executable.resolve())] if args.executable else [sys.executable,str(ROOT/'run_magic_converter.py')]
        for label, blend, isolated in [('multiply',True,None),('normal',False,None),
                                       ('isolated-nested',True,True),('nonisolated-nested',True,False)]:
            source=root/(label+'.pdf')
            if isolated is None: effect_pdf(source,blend=blend)
            else: isolated_effect_pdf(source,isolated=isolated)
            before=sha(source);job_path=root/(label+'-job.json')
            done=subprocess.run(prefix+['convert',str(source),'--action','pptx_editable','--output',str(root/label),
                 '--report',str(job_path),'--data-dir',str(root/'profile'),'--timeout','60','--json'],
                 capture_output=True,timeout=120,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
            job=json.loads(job_path.read_text(encoding='utf-8-sig'))
            if done.returncode or not job['ok']: raise ValueError(str(job))
            target=Path(job['jobs'][0]['outputs'][0]);deck=Presentation(target)
            shapes=deck.slides[0].shapes
            counts={str(kind):sum(s.shape_type==kind for s in shapes) for kind in (1,13,17)}
            case={'mode':label,'source_sha256':before,'shape_counts':counts};report['cases'].append(case);save()
            expected_paths = int(isolated) if isolated is not None else (0 if blend else 1)
            expected_text = (['Independent title'] if isolated else []) if isolated is not None else ['Independent before','Independent after']
            if (counts['1'] != expected_paths or counts['13'] != 1
                    or [s.text for s in shapes if s.has_text_frame and s.text] != expected_text):
                raise ValueError('Duplicated blend backdrop or flattened independent text')
            if args.office:
                native=root/(label+'-native.pdf')
                case['native_export']=export_pdf(target,native,root/(label+'-native-export'),args.executable)
                case['visual']=compare(source,native,artifacts=root/(label+'-visual'))
                if case['visual']['pages'][0]['mae'] >= 1: raise ValueError('Native blend pixels changed')
                for shape in list(shapes):
                    if shape.shape_type==13: shape._element.getparent().remove(shape._element)
                edited=root/(label+'-edited.pptx');deck.save(edited)
                deleted=root/(label+'-deleted.pdf')
                case['deletion_export']=export_pdf(edited,deleted,root/(label+'-deletion-export'),args.executable)
                with fitz.open(deleted) as pdf:
                    pixel=pdf[0].get_pixmap().pixel(80,110)
                    expected=((179,217,255) if isolated else (255,255,255)) if isolated is not None else ((255,255,255) if blend else (255,128,128))
                    if max(abs(a-b) for a,b in zip(pixel,expected)) > 3: raise ValueError('Deleted effect left raster ghost')
                    if expected_text and 'Independent' not in pdf[0].get_text(): raise ValueError('Independent text disappeared')
                    case['deleted_centre_rgb']=pixel
                case['native_office']='passed'
                if isolated is not None:
                    wrong=root/(label+'-wrong-blend.pdf')
                    isolated_effect_pdf(wrong,isolated=isolated,blend=False)
                    with fitz.open(wrong) as bad, fitz.open(native) as good:
                        a=bad[0].get_pixmap().pixel(100,110)
                        b=good[0].get_pixmap().pixel(100,110)
                    case['wrong_blend_rgb_delta']=max(abs(x-y) for x,y in zip(a,b))
                    if case['wrong_blend_rgb_delta'] < 10:
                        raise ValueError('Lost multiply negative control was not distinguished')
            if sha(source)!=before: raise ValueError('Original PDF changed')
            case['status']='passed';save()
        if args.office: report['native_office']='passed'
        report['status']='passed'
    except Exception as error: report.update(status='failed',error=str(error))
    save();print(json.dumps(report,ensure_ascii=False,indent=2))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__': raise SystemExit(main())
