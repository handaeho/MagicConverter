"""Source/frozen tight single-line cells, normal/empty controls and native pixels."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from pptx import Presentation
from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import single_line_cells_pdf
from magic_converter.core.diagnostics.visual_comparison import compare,sha
from tools.validation.native_office_export import export_pdf

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path);parser.add_argument('--baseline',type=Path);parser.add_argument('--office',action='store_true')
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'cases':{},'native_office':'pending'if args.office else'skipped'}
    def save():(out/'report.json').write_text(json.dumps(report,indent=2)+'\n',encoding='utf-8')
    def convert(source,folder,exe):
        folder.mkdir();job=folder/'job.json';prefix=[str(exe.resolve())]if exe else[sys.executable,str(ROOT/'run_magic_converter.py')]
        done=subprocess.run(prefix+['convert',str(source),'--action','pptx_editable','--output',str(folder/'output'),
             '--report',str(job),'--data-dir',str(folder/'profile'),'--timeout','60','--json'],capture_output=True,timeout=120,
             env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'conversion.log').write_bytes(done.stdout+done.stderr);result=json.loads(job.read_text(encoding='utf-8-sig'))
        if done.returncode or not result['ok']:raise ValueError(str(result))
        return Path(result['jobs'][0]['outputs'][0])
    save()
    try:
        for name,options,expected in [('tight',{},4),('loose',{'row_height':60},0),('empty',{'empty':True},0)]:
            folder=out/name;folder.mkdir();source=folder/'source.pdf';single_line_cells_pdf(source,**options);original=sha(source)
            target=convert(source,folder/'current',args.executable);deck=Presentation(target);shapes=deck.slides[0].shapes
            positioned=[s for s in shapes if s.name.startswith('PDF cell R')]
            case={'status':'running','source_sha256':original,'positioned_textboxes':len(positioned)};report['cases'][name]=case;save()
            if len(positioned)!=expected:raise ValueError('Single-line baseline preservation changed')
            if sum(s.has_table for s in shapes)!=int(name!='empty'):raise ValueError('Table/grid control changed')
            if not any(s.has_text_frame and s.text=='Independent title'for s in shapes):raise ValueError('Independent title lost')
            if args.office:
                native=folder/'native.pdf';case['native_export']=export_pdf(target,native,folder/'export',args.executable)
                case['visual']=compare(source,native,dpi=144,artifacts=folder/'visual')
                if 'mae'not in case['visual']['pages'][0]:raise ValueError('Native canvas not comparable: '+case['visual']['pages'][0].get('reason','unknown'))
                if case['visual']['pages'][0]['mae']>=2:raise ValueError('Cell page pixel ceiling exceeded')
                if args.baseline:
                    previous=convert(source,folder/'baseline',args.baseline);old_pdf=folder/'baseline.pdf'
                    case['baseline_export']=export_pdf(previous,old_pdf,folder/'baseline-export',args.baseline)
                    case['baseline_visual']=compare(source,old_pdf,dpi=144)
                    case['mae_delta']=case['visual']['pages'][0]['mae']-case['baseline_visual']['pages'][0]['mae']
                    if case['mae_delta']>1e-6:raise ValueError('Cell pixels regressed against actual baseline EXE')
                    if name=='tight'and case['mae_delta']>=-.01:raise ValueError('Tight-cell baseline defect was not reproduced')
                if name=='tight':
                    for shape in list(shapes):
                        if shape.name.startswith('PDF cell R'):shape._element.getparent().remove(shape._element)
                    edited=folder/'deleted.pptx';deck.save(edited)
                    case['deletion_export']=export_pdf(edited,folder/'deleted.pdf',folder/'deleted-export',args.executable)
                    import pymupdf as fitz
                    with fitz.open(folder/'deleted.pdf')as pdf:
                        if 'Cell'in pdf[0].get_text()or'Independent title'not in pdf[0].get_text():raise ValueError('Cell deletion left text or removed independent title')
            if sha(source)!=original:raise ValueError('Original source changed')
            case['status']='passed';save()
        report['status']='passed';report['native_office']='passed'if args.office else'skipped'
    except Exception as exc:report.update(status='failed',error=f'{type(exc).__name__}: {exc}')
    save();print(json.dumps(report,indent=2));return 0 if report['status']=='passed'else 1
if __name__=='__main__':raise SystemExit(main())
