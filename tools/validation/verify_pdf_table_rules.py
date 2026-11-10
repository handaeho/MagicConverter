"""Per-border styles and actual Office pixels, including a dashed negative."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from pptx import Presentation
from pptx.oxml.ns import qn
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import compare,sha
from tools.validation.native_office_export import export_pdf
from tests.pdf.test_table_rule_styles import fixture


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path);parser.add_argument('--office',action='store_true')
    parser.add_argument('--baseline',type=Path);args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'cases':{},'native_office':'requested'if args.office else'not_requested',
            'executable_sha256':sha(args.executable)if args.executable else None}
    def save():(out/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    def convert(source,folder,executable):
        prefix=[str(executable.resolve())]if executable else[sys.executable,str(ROOT/'run_magic_converter.py')]
        jobfile=folder/'job.json';folder.mkdir()
        done=subprocess.run(prefix+['convert',str(source),'--action','pptx_editable','--output',str(folder/'output'),
             '--report',str(jobfile),'--data-dir',str(folder/'profile'),'--timeout','60','--json'],capture_output=True,timeout=120,
             env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'conversion.log').write_bytes(done.stdout+done.stderr)
        job=json.loads(jobfile.read_text(encoding='utf-8-sig'))
        if done.returncode or not job['ok']:raise ValueError('Conversion failed: '+str(job))
        return Path(job['jobs'][0]['outputs'][0])
    save()
    try:
        for name,options in [('mixed',{}),('normal',{'width':.5}),('dashed',{'width':.5,'dashed':True})]:
            folder=out/name;folder.mkdir();source=folder/'source.pdf';fixture(source,**options);original=sha(source)
            target=convert(source,folder/'current',args.executable);deck=Presentation(target)
            tables=[s.table for s in deck.slides[0].shapes if s.has_table]
            expected=0 if name=='dashed'else 1
            if len(tables)!=expected:raise ValueError('Table safety boundary changed')
            case={'status':'running','source_sha256':original,'tables':len(tables)};report['cases'][name]=case;save()
            if name=='mixed':
                left=tables[0].cell(0,0)._tc.xpath('./a:tcPr/a:lnL')[0]
                right=tables[0].cell(1,1)._tc.xpath('./a:tcPr/a:lnR')[0]
                if abs(int(left.get('w'))/12700-.1)>1e-4 or int(right.get('w'))!=38100:
                    raise ValueError('Original border widths were clamped')
                if right.find(qn('a:solidFill')).find(qn('a:srgbClr')).get('val')!='FF0000':raise ValueError('Border colour lost')
                case['per_edge_styles']='passed'
            if args.office:
                native=folder/'native.pdf';case['native_export']=export_pdf(target,native,folder/'export',args.executable)
                case['visual']=compare(source,native,dpi=144,artifacts=folder/'visual')
                if case['visual']['pages'][0]['mae']>=2:raise ValueError('Table page pixel ceiling exceeded')
                if args.baseline:
                    old=convert(source,folder/'baseline',args.baseline);old_pdf=folder/'baseline.pdf'
                    case['baseline_export']=export_pdf(old,old_pdf,folder/'baseline-export',args.baseline)
                    case['baseline_visual']=compare(source,old_pdf,dpi=144)
                    delta=case['visual']['pages'][0]['mae']-case['baseline_visual']['pages'][0]['mae'];case['mae_delta']=delta
                    if name=='mixed'and delta>=-.1:raise ValueError('Mixed rules did not improve')
                    if name!='dashed'and delta>1e-6:raise ValueError('Supported table control pixels worsened')
                    if name=='dashed':
                        case['comparison_policy']='Unsupported semantic dash style keeps original paths; old solid-table score is measured, not a style-fidelity oracle'
                # Removing the table must remove all rules and cell text.
                if tables:
                    shape=next(s for s in deck.slides[0].shapes if s.has_table)
                    shape._element.getparent().remove(shape._element);deleted=folder/'deleted.pptx';deck.save(deleted)
                    case['deletion_export']=export_pdf(deleted,folder/'deleted.pdf',folder/'delete-export',args.executable)
                    import pymupdf as fitz
                    with fitz.open(folder/'deleted.pdf')as pdf:
                        if min(pdf[0].get_pixmap(alpha=False).samples)<250:raise ValueError('Deleted table left ink')
                    case['deletion']='passed'
            if sha(source)!=original:raise ValueError('Original changed')
            case['status']='passed';save()
        report['status']='passed'
    except Exception as exc:report.update(status='failed',error=type(exc).__name__+': '+str(exc))
    save();print(json.dumps(report,indent=2));return int(report['status']!='passed')

if __name__=='__main__':raise SystemExit(main())
