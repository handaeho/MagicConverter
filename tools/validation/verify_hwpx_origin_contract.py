"""Installed private faces and raw declared-margin coordinates; no inferred shift."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import pymupdf as fitz
from magic_converter import __version__
from magic_converter.core.documents.hwp_documents import DocumentData,Paragraph,TextRun,write_hwpx
from magic_converter.core.engines.rhwp_engine import font_directories
from magic_converter.core.diagnostics.visual_comparison import sha


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path);parser.add_argument('--native',action='store_true')
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'executable_sha256':sha(args.executable)if args.executable else None,
            'coordinate_contract':'Raw PDF span origin; no alignment, translation, rescaling or changed default printer',
            'cases':[],'native':'requested'if args.native else'skipped: not requested'}
    def save():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def convert(source,folder,native=False):
        profile=folder/'profile';folder.mkdir()
        if native:
            from magic_converter.core.settings.preferences import default_preferences
            from magic_converter.core.settings.settings_store import SettingsStore
            pass
        prefix=[str(args.executable.resolve())]if args.executable else[sys.executable,str(ROOT/'run_magic_converter.py')]
        done=subprocess.run(prefix+['convert',str(source),'--action','document:pdf','--output',str(folder/'output'),
              '--data-dir',str(profile),'--report',str(folder/'job.json'),'--json','--timeout','120'],capture_output=True,timeout=180,
              env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'conversion.log').write_bytes(done.stdout+done.stderr);job=json.loads((folder/'job.json').read_text(encoding='utf-8-sig'))
        if done.returncode or not job['ok']:raise ValueError(str(job))
        if native and any('rhwp'in w or '대체'in w for w in job['jobs'][0].get('warnings',[])):
            raise ValueError('Native comparison silently fell back')
        with fitz.open(job['jobs'][0]['outputs'][0])as pdf:
            spans=[s for b in pdf[0].get_text('dict')['blocks']if 'lines'in b for l in b['lines']for s in l['spans']]
            if not spans or 'Origin'not in ''.join(s['text']for s in spans):raise ValueError('Missing text control')
            return {'pages':len(pdf),'origin_pt':list(spans[0]['origin']),'fonts':sorted({s['font']for s in spans}),
                    'media_box_pt':list(pdf[0].mediabox),'crop_box_pt':list(pdf[0].cropbox),'rotation':pdf[0].rotation}
    save()
    try:
        from magic_converter.core.diagnostics.printer_geometry import default_printer_geometry,predicted_native_origin
        printer=default_printer_geometry()if args.native else {'status':'not_requested'}
        report['printer_geometry']=printer
        private=any((p/'HDOTUM.TTF').is_file()for p in font_directories())
        pass
        for margin in(36,72,108):
            folder=out/str(margin);folder.mkdir();source=folder/'control.hwpx'
            pass
            original=sha(source);portable=convert(source,folder/'fallback')
            if abs(portable['origin_pt'][0]-margin)>.1:raise ValueError('Fallback does not honor declared left margin')
            pass
            case={'declared_left_margin_pt':margin,'declared_top_margin_pt':margin,'input_sha256':original,'fallback':portable}
            if args.native:
                native=convert(source,folder/'native',True);case['native']=native
                case['fallback_minus_native_origin_pt']=[a-b for a,b in zip(portable['origin_pt'],native['origin_pt'])]
                if printer['status']=='measured':
                    predicted=predicted_native_origin(portable['origin_pt'],portable['media_box_pt'][2:],native['media_box_pt'][2:],printer['hard_margin_pt'])
                    case['printer_geometry_prediction']={'origin_pt':predicted,'residual_pt':[a-b for a,b in zip(native['origin_pt'],predicted)],'applied_to_output':False}
                pass
            if sha(source)!=original:raise ValueError('Original control modified')
            report['cases'].append(case);save()
        if args.native:
            report['printer_geometry_after']=default_printer_geometry()
            if report['printer_geometry_after']!=printer:raise ValueError('Default printer geometry changed during controls')
            if printer['status']=='measured':
                report['printer_offset_explanation']={'max_raw_residual_pt':max(abs(v)for c in report['cases']for v in c['printer_geometry_prediction']['residual_pt']),
                    'scope':'Independent device hard margins plus page-size scaling, diagonal margin controls on this host; no output correction or universal device claim',
                    'reference':'https://learn.microsoft.com/en-us/windows/win32/api/wingdi/nf-wingdi-getdevicecaps'}
            cases=report['cases'];first,last=cases[0],cases[-1]
            scale=(last['native']['origin_pt'][0]-first['native']['origin_pt'][0])/(last['declared_left_margin_pt']-first['declared_left_margin_pt'])
            intercept=first['native']['origin_pt'][0]-scale*first['declared_left_margin_pt']
            report['observed_native_horizontal_mapping']={'scale':scale,'intercept_pt':intercept,
                'max_residual_pt':max(abs(c['native']['origin_pt'][0]-(scale*c['declared_left_margin_pt']+intercept))for c in cases),
                'constant_12pt_hypothesis_max_residual_pt':max(abs(c['native']['origin_pt'][0]-(c['declared_left_margin_pt']-12))for c in cases),
                'applied_to_output':False,'scope':'Three generated controls in this host; not a universal device/driver correction'}
        report.update(status='passed',native='measured'if args.native else'skipped: not requested',
                      limitation='Native raw coordinate differences are measured, not corrected or certified equivalent. Actual 33-page spacing/font differences remain separate.')
    except Exception as exc:report.update(status='failed',error=type(exc).__name__+': '+str(exc))
    save();print(json.dumps(report,ensure_ascii=True));return int(report['status']!='passed')

if __name__=='__main__':raise SystemExit(main())
