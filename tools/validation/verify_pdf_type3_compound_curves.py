"""Compound Bezier Type3 topology and complete-replay negative controls."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import pymupdf as fitz
from pptx import Presentation
from magic_converter import __version__
from magic_converter.core.diagnostics.pdf_fixtures import type3_compound_curve_pdf, type3_disjoint_curve_pdf
from magic_converter.core.diagnostics.visual_comparison import compare,sha
from tools.validation.native_office_export import export_pdf

CONCAVE_CASES=('disjoint','reversed','mixed','masked','gradient','partial','touching','near-touching',
               'overlap','nested','nested-hole','crossing','curved-hole','curved-same','curved-island','pocket')
CASES=('hole','same-winding','island','disjoint','mixed','ambiguous','refined-same-winding','nested-lens',
       'touching','overlap','near-touching','masked','masked-gradient','partial','nonconvex','masked-nonconvex')
CASES+=tuple('concave-'+name for name in CONCAVE_CASES)
CASES+=('masked-tiled','masked-overlap','masked-gap','masked-different')
CASES+=('masked-disjoint-glyphs','masked-overlapping-glyphs','masked-touching-glyphs','masked-near-glyphs')
# Expected original contour/cubic counts, independent of the topology proof.
EDITABLE={'hole':(2,8),'same-winding':(1,4),'island':(3,12),'disjoint':(2,8),'mixed':(2,4),
          'ambiguous':(2,8),'refined-same-winding':(1,4),'nested-lens':(2,6),'masked':(2,8),
          'nonconvex':(1,1),'masked-nonconvex':(1,1),
          'concave-disjoint':(2,2),'concave-reversed':(2,2),'concave-mixed':(2,1),'concave-masked':(2,2),
          'concave-nested':(1,1),'concave-nested-hole':(2,1),'concave-curved-hole':(2,2),
          'concave-curved-same':(1,1),'concave-curved-island':(3,2),'concave-pocket':(2,1),
          'masked-tiled':(2,8),'masked-overlap':(2,8),'masked-disjoint-glyphs':(4,16)}

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path);parser.add_argument('--baseline',type=Path);parser.add_argument('--office',action='store_true')
    parser.add_argument('--cases',nargs='+',choices=CASES)
    args=parser.parse_args();root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'native_office':'requested'if args.office else'skipped',
            'executable_sha256':sha(args.executable)if args.executable else None,
            'baseline_sha256':sha(args.baseline)if args.baseline else None,'cases':{}}
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
        for name in (args.cases or CASES):
            folder=root/name;folder.mkdir();source=folder/'source.pdf'
            if name.startswith('concave-'):type3_disjoint_curve_pdf(source,name.removeprefix('concave-'))
            else:type3_compound_curve_pdf(source,name)
            original=sha(source)
            target=convert(source,folder/'converted',args.executable);deck=Presentation(target);shapes=deck.slides[0].shapes
            groups=[s for s in shapes if s.name.startswith('PDF glyph outline region ')]
            case={'status':'running','source_sha256':original,'pictures':sum(s.shape_type==13 for s in shapes),'groups':len(groups)}
            report['cases'][name]=case;save()
            if 'Independent title'not in[s.text for s in shapes if s.has_text_frame]:raise ValueError('Independent title flattened/lost')
            if name in EDITABLE:
                if case['pictures']or len(groups)!=1 or len(groups[0].shapes)!=1:raise ValueError('Simple nonzero topology not editable')
                expected,cubics=EDITABLE[name]
                case['kept_contours']=len(groups[0].shapes[0]._element.xpath('.//a:moveTo'))
                case['cubic_commands']=len(groups[0].shapes[0]._element.xpath('.//a:cubicBezTo'))
                if case['cubic_commands']!=cubics:raise ValueError('Original cubic commands lost')
                if case['kept_contours']!=expected:raise ValueError('Nonzero coverage boundaries differ')
                if name in {'concave-masked','masked-disjoint-glyphs'}and groups[0].shapes[0]._element.xpath('.//a:alpha')[0].get('val')!='50000':
                    raise ValueError('Uniform mask alpha lost')
            elif case['pictures']!=1 or groups:raise ValueError('Unsafe/complex paint did not keep complete replay')
            if args.office:
                native=folder/'native.pdf';case['native_export']=export_pdf(target,native,folder/'native-export',args.executable)
                case['dpi']={}
                for dpi in (72,96,144,200,300):
                    visual=compare(source,native,dpi=dpi,artifacts=folder/'visual'if dpi==144 else None)
                    if 'mae'not in visual['pages'][0]or visual['pages'][0]['mae']>=1:raise ValueError('Native topology/control mismatch')
                    case['dpi'][str(dpi)]=visual['pages'][0]
                if args.baseline and (name in EDITABLE or name.startswith('masked-') or name.startswith('concave-')):
                    old=convert(source,folder/'baseline',args.baseline);old_native=folder/'baseline.pdf'
                    old_shapes=Presentation(old).slides[0].shapes
                    case['baseline_pictures']=sum(s.shape_type==13 for s in old_shapes)
                    case['baseline_groups']=sum(s.name.startswith('PDF glyph outline region ')for s in old_shapes)
                    if name=='masked-disjoint-glyphs' and (case['baseline_pictures']!=1 or case['baseline_groups']):
                        raise ValueError('Baseline masked multi-glyph fallback was not reproduced')
                    case['baseline_export']=export_pdf(old,old_native,folder/'baseline-export',args.baseline)
                    case['baseline_dpi']={str(dpi):compare(source,old_native,dpi=dpi)['pages'][0]for dpi in(72,96,144,200,300)}
                    if name.startswith('concave-') and name not in EDITABLE:
                        if case['baseline_pictures']!=1 or case['baseline_groups']:
                            raise ValueError('Baseline unsafe paint did not keep complete replay')
                        case['baseline_output_dpi']={str(dpi):compare(old_native,native,dpi=dpi)['pages'][0]for dpi in(72,96,144,200,300)}
                        if any(page.get('mae')!=0 for page in case['baseline_output_dpi'].values()):
                            raise ValueError('Unmodified concave fallback pixels changed')
                    if name in {'masked-overlapping-glyphs','masked-touching-glyphs','masked-near-glyphs'}:
                        if case['baseline_pictures']!=1 or case['baseline_groups']:
                            raise ValueError('Baseline unsafe masked glyphs did not keep replay')
                        case['baseline_output_dpi']={str(dpi):compare(old_native,native,dpi=dpi)['pages'][0]for dpi in(72,96,144,200,300)}
                        if any(page.get('mae')!=0 for page in case['baseline_output_dpi'].values()):
                            raise ValueError('Unmodified masked glyph fallback pixels changed')
                    if name in {'masked','masked-gradient'}and any(case['dpi'][str(dpi)]['mae']>case['baseline_dpi'][str(dpi)]['mae']+1e-6 for dpi in(72,96,144,200,300)):
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
