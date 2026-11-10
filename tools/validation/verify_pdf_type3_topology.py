"""Linear Type3 nonzero topology and complete-replay negative controls."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import pymupdf as fitz
from pptx import Presentation
from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import type3_topology_pdf
from magic_converter.core.diagnostics.visual_comparison import compare,sha
from tools.validation.native_office_export import export_pdf

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path);parser.add_argument('--baseline',type=Path);parser.add_argument('--office',action='store_true')
    args=parser.parse_args();root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'native_office':'requested'if args.office else'skipped',
            'executable_sha256':sha(args.executable)if args.executable else None,'cases':{}}
    def save():(root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    def convert(source,folder,executable):
        job_path=folder.with_suffix('.json');prefix=[str(executable.resolve())]if executable else[sys.executable,str(ROOT/'run_magic_converter.py')]
        done=subprocess.run(prefix+['convert',str(source),'--action','pptx_editable','--output',str(folder),
            '--report',str(job_path),'--data-dir',str(root/'profile'),'--timeout','60','--json'],capture_output=True,timeout=120,
            env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        job=json.loads(job_path.read_text(encoding='utf-8-sig'))
        if done.returncode or not job['ok']:raise ValueError(str(job))
        return Path(job['jobs'][0]['outputs'][0])
    save()
    try:
        for name in ('concave','hole','same-winding','island','disjoint','touching','crossing','retraced','masked','partial'):
            folder=root/name;folder.mkdir();source=folder/'source.pdf';type3_topology_pdf(source,name);original=sha(source)
            target=convert(source,folder/'converted',args.executable);deck=Presentation(target);shapes=deck.slides[0].shapes
            groups=[s for s in shapes if s.name.startswith('PDF glyph outline region ')]
            case={'status':'running','source_sha256':original,'pictures':sum(s.shape_type==13 for s in shapes),'groups':len(groups)}
            report['cases'][name]=case;save()
            if 'Independent title'not in[s.text for s in shapes if s.has_text_frame]:raise ValueError('Independent title flattened/lost')
            if name in {'concave','hole','same-winding','island','disjoint','masked'}:
                if case['pictures']or len(groups)!=1 or len(groups[0].shapes)!=1:raise ValueError('Simple nonzero topology not editable')
                expected={'concave':1,'hole':2,'same-winding':1,'island':3,'disjoint':2,'masked':1}[name]
                case['kept_contours']=len(groups[0].shapes[0]._element.xpath('.//a:moveTo'))
                if case['kept_contours']!=expected:raise ValueError('Nonzero coverage boundaries differ')
            elif case['pictures']!=1 or groups:raise ValueError('Unsafe/complex paint did not keep complete replay')
            if args.office:
                native=folder/'native.pdf';case['native_export']=export_pdf(target,native,folder/'native-export',args.executable)
                case['dpi']={}
                for dpi in (72,96,144,200,300):
                    visual=compare(source,native,dpi=dpi,artifacts=folder/'visual'if dpi==144 else None)
                    if 'mae'not in visual['pages'][0]or visual['pages'][0]['mae']>=1:raise ValueError('Native topology/control mismatch')
                    case['dpi'][str(dpi)]=visual['pages'][0]
                if args.baseline and name in {'concave','hole','same-winding','island','disjoint','masked'}:
                    old=convert(source,folder/'baseline',args.baseline);old_native=folder/'baseline.pdf'
                    case['baseline_export']=export_pdf(old,old_native,folder/'baseline-export',args.baseline)
                    case['baseline_dpi']={str(dpi):compare(source,old_native,dpi=dpi)['pages'][0]for dpi in(72,96,144,200,300)}
                    if name=='masked'and any(case['dpi'][str(dpi)]['mae']>case['baseline_dpi'][str(dpi)]['mae']+1e-6 for dpi in(72,96,144,200,300)):
                        raise ValueError('Unmodified mask preservation regressed')
                for shape in list(shapes):
                    if shape.shape_type==13 or shape in groups:shape._element.getparent().remove(shape._element)
                deleted=folder/'deleted.pptx';deck.save(deleted);deleted_pdf=folder/'deleted.pdf'
                case['deletion_export']=export_pdf(deleted,deleted_pdf,folder/'deletion-export',args.executable)
                with fitz.open(deleted_pdf)as pdf:
                    if 'Independent title'not in pdf[0].get_text()or min(pdf[0].get_pixmap(clip=fitz.Rect(0,60,180,180)).samples)<250:
                        raise ValueError('Deleted Type3 left ghost/lost title')
            if sha(source)!=original:raise ValueError('Original generated input changed')
            case['status']='passed';save()
        report['status']='passed'
        if args.office:report['native_office']='passed'
    except Exception as error:report.update(status='failed',error=f'{type(error).__name__}: {error}')
    save();print(json.dumps(report));return 0 if report['status']=='passed'else 1

if __name__=='__main__':raise SystemExit(main())
