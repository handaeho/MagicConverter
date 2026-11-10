"""Source/frozen glyph transforms, normal editable text and Office deletion."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import pymupdf as fitz
from pptx import Presentation
from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import glyph_scale_pdf
from magic_converter.core.diagnostics.visual_comparison import compare,sha
from tools.validation.native_office_export import export_pdf


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path);parser.add_argument('--office',action='store_true')
    args=parser.parse_args();root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'executable_sha256':sha(args.executable) if args.executable else None,
            'native_office':'pending' if args.office else 'skipped',
            'fixture_mae_ceiling':1.5,'fixture_144dpi_mae_ceiling':.9,
            'note':'Grouped original outlines; geometry/deletion gate, strict visual differences remain separately recorded.'}
    def save():(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        source=root/'glyphs.pdf';glyph_scale_pdf(source);before=sha(source)
        env=os.environ.copy();env['PYTHONUTF8']='1'
        env['PATH']=str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32')
        for key in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH'):env.pop(key,None)
        prefix=[str(args.executable.resolve())] if args.executable else [sys.executable,str(ROOT/'run_magic_converter.py')]
        job_path=root/'job.json'
        result=subprocess.run(prefix+['convert',str(source),'--action','pptx_editable','--output',str(root/'output'),
            '--report',str(job_path),'--data-dir',str(root/'profile'),'--timeout','60','--json'],
            capture_output=True,timeout=120,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        job=json.loads(job_path.read_text(encoding='utf-8-sig'))
        if result.returncode or not job['ok']:raise ValueError(str(job))
        target=Path(job['jobs'][0]['outputs'][0]);deck=Presentation(target);shapes=deck.slides[0].shapes
        if ([s.text for s in shapes if s.has_text_frame]!=['Glyph widths WWW iii']
                or sum(s.name.startswith('PDF glyph outline region ') for s in shapes)!=2
                or any(s.shape_type==13 for s in shapes)):raise ValueError('Unsupported glyph transform lost its grouped original outline')
        outlined=deck.slides[1].shapes
        if ([s.text for s in outlined if s.has_text_frame]!=['Independent before','Independent after']
                or sum(s.name.startswith('PDF glyph outline region ') for s in outlined)!=1
                or any(s.shape_type==13 for s in outlined)):raise ValueError('Stroke callback discarded its outline or independent text')
        report['structure']='passed';report['warnings']=job['jobs'][0].get('warnings',[])
        if args.office:
            native=root/'native.pdf'
            report['native_export']=export_pdf(target,native,root/'native-export',args.executable)
            report['visual']=compare(source,native,artifacts=root/'visual')
            if any(p['mae']>=report['fixture_mae_ceiling'] for p in report['visual']['pages']):raise ValueError('Glyph fixture pixel ceiling exceeded')
            report['visual_144dpi']=compare(source,native,dpi=144,artifacts=root/'visual-144dpi')
            if any(p['mae']>=report['fixture_144dpi_mae_ceiling'] for p in report['visual_144dpi']['pages']):raise ValueError('144dpi glyph ceiling exceeded')
            for slide in deck.slides:
                for shape in list(slide.shapes):
                    if shape.shape_type==13 or shape.name.startswith('PDF glyph outline region '):
                        shape._element.getparent().remove(shape._element)
            edited=root/'deleted.pptx';deck.save(edited)
            report['deletion_export']=export_pdf(edited,root/'deleted.pdf',root/'deletion-export',args.executable)
            with fitz.open(root/'deleted.pdf') as pdf:
                if 'Glyph widths' not in pdf[0].get_text():raise ValueError('Normal text disappeared')
                if min(pdf[0].get_pixmap(clip=fitz.Rect(0,80,400,240)).samples)<250:raise ValueError('Deleted glyph picture left ink')
                if 'Independent before' not in pdf[1].get_text() or 'Independent after' not in pdf[1].get_text():raise ValueError('Outline removed independent text')
                if min(pdf[1].get_pixmap(clip=fitz.Rect(0,80,400,155)).samples)<250:raise ValueError('Deleted outline left ink')
            report['negative_visual']=compare(source,root/'deleted.pdf')
            if max(p['mae'] for p in report['negative_visual']['pages'])<report['fixture_mae_ceiling']:raise ValueError('Missing-outline negative control did not fail the pixel gate')
            report.update(native_office='passed',deletion='passed')
        if sha(source)!=before:raise ValueError('Original changed')
        report.update(status='passed',source_sha256=before)
    except Exception as error:report.update(status='failed',error=str(error))
    save();print(json.dumps(report,ensure_ascii=False,indent=2));return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
