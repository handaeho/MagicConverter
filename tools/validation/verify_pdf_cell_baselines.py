"""Real Office cell baselines, a broken-leading negative and independent text."""
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
from pptx.util import Pt
from magic_converter import __version__
from magic_converter.core.pdf.pdf_fonts import resolve_font
from magic_converter.core.diagnostics.visual_comparison import compare,sha
from magic_converter.core.diagnostics.pdf_fixtures import dense_table_pdf
from tools.validation.native_office_export import export_pdf


def fixture(path):
    face=resolve_font('Arial')
    if face is None:raise RuntimeError('Arial is required for the real Office geometry fixture')
    with fitz.open() as pdf:
        page=pdf.new_page(width=600,height=420)
        page.insert_font(fontname='local',fontfile=str(face.path))
        for x in (20,300,580):page.draw_line((x,30),(x,390),width=.5)
        for y in (30,150,270,390):page.draw_line((20,y),(580,y),width=.5)
        for x,text in ((32,'Ordinary left'),(318,'Right column')):
            page.insert_text((x,65),text,fontname='local',fontsize=14 if x==32 else 8)
            page.insert_text((x,102),'Second spaced line',fontname='local',fontsize=14 if x==32 else 11.5)
        page.insert_text((32,200),'Large ',fontname='local',fontsize=20)
        # Same baseline but a smaller run, positioned at the larger run's end.
        font=fitz.Font(fontfile=str(face.path))
        page.insert_text((32+font.text_length('Large ',fontsize=20),200),'small',fontname='local',fontsize=10)
        page.insert_text((318,200),'Single normal',fontname='local',fontsize=12)
        page.insert_text((32,310),'Last row',fontname='local',fontsize=12)
        page.insert_text((32,363),'Uneven baseline gap',fontname='local',fontsize=9.5)
        page.insert_text((318,330),'Normal control',fontname='local',fontsize=12)
        page.insert_text((24,410),'Independent text outside the table',fontname='local',fontsize=10)
        pdf.save(path)


def positions(source,candidate,dense=False):
    def chars(page,bounds):
        return [(c['c'],c['origin']) for b in page.get_text('rawdict',clip=bounds)['blocks'] if b['type']==0
                for line in b['lines'] for span in line['spans'] for c in span['chars'] if not c['c'].isspace()]
    records=[]
    with fitz.open(source) as before,fitz.open(candidate) as after:
        if len(before)!=len(after):raise ValueError('Page count changed')
        rows=((20,220),(220,420)) if dense else ((30,150),(150,270),(270,390))
        columns=((20,225),(225,430),(430,640)) if dense else ((20,300),(300,580))
        for row,(top,bottom) in enumerate(rows):
            for col,(left,right) in enumerate(columns):
                aa,bb=chars(before[0],(left,top,right,bottom)),chars(after[0],(left,top,right,bottom))
                if ''.join(c[0] for c in aa)!=''.join(c[0] for c in bb):raise ValueError('Cell characters changed')
                records.append({'row':row,'col':col,'characters':len(aa),
                                'max_baseline_error_pt':max(abs(a[1][1]-b[1][1]) for a,b in zip(aa,bb))})
        if 'Independent text outside the table' not in after[0].get_text():raise ValueError('Independent text lost')
    return records


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True,type=Path)
    parser.add_argument('--dense',action='store_true')
    parser.add_argument('--executable',type=Path);args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'max_baseline_error_pt':.6,
            'executable_sha256':sha(args.executable) if args.executable else None,'dense':args.dense,
            'negative_control':'force 10pt leading' if args.dense else 'remove inter-paragraph gaps'}
    def save():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        source=out/'source.pdf';(dense_table_pdf if args.dense else fixture)(source);before=sha(source)
        prefix=[str(args.executable.resolve())] if args.executable else [sys.executable,'-B',str(ROOT/'run_magic_converter.py')]
        env={**os.environ,'PYTHONUTF8':'1','PYTHONDONTWRITEBYTECODE':'1'}
        jobfile=out/'job.json'
        done=subprocess.run([*prefix,'convert',str(source),'--action','pptx_editable','--output',str(out/'output'),
                             '--report',str(jobfile),'--data-dir',str(out/'profile'),'--timeout','60','--json'],
                            capture_output=True,env=env,timeout=120,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        job=json.loads(jobfile.read_text(encoding='utf-8-sig'))
        if done.returncode or not job['ok']:raise ValueError(str(job))
        target=Path(job['jobs'][0]['outputs'][0]);deck=Presentation(target)
        tables=[s.table for s in deck.slides[0].shapes if s.has_table]
        expected=(2,3) if args.dense else (3,2)
        if len(tables)!=1 or (len(tables[0].rows),len(tables[0].columns))!=expected:raise ValueError('Native table structure changed')
        native=out/'native.pdf';report['native_export']=export_pdf(target,native,out/'native-export',args.executable)
        report['cells']=positions(source,native,args.dense)
        if any(c['max_baseline_error_pt']>=.6 for c in report['cells']):raise ValueError('Stored cell baselines moved')
        report['visual']=compare(source,native,artifacts=out/'visual')
        # Deliberate failure: discard stored gaps, or compress dense leading.
        for cell in tables[0].iter_cells():
            for p in cell.text_frame.paragraphs:
                if args.dense:p.line_spacing=Pt(10)
                else:p.space_after=Pt(0)
        broken=out/'broken.pptx';deck.save(broken)
        report['negative_export']=export_pdf(broken,out/'broken.pdf',out/'negative-export',args.executable)
        report['negative_cells']=positions(source,out/'broken.pdf',args.dense)
        if max(c['max_baseline_error_pt'] for c in report['negative_cells'])<10:raise ValueError('Negative control did not expose lost line gaps')
        if sha(source)!=before:raise ValueError('Original input changed')
        report.update(status='passed',source_sha256=before)
    except Exception as error:report.update(status='failed',error=str(error))
    save();print(json.dumps({'status':report['status'],'error':report.get('error'),'cells':report.get('cells')}))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
