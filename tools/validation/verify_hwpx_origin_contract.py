"""Check raw declared margins through the rhwp PDF renderer."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import pymupdf as fitz
from magic_converter import __version__
from magic_converter.core.documents.hwp_documents import DocumentData,Paragraph,TextRun,write_hwpx
from magic_converter.core.diagnostics.visual_comparison import sha


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path)
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'executable_sha256':sha(args.executable)if args.executable else None,
            'coordinate_contract':'Raw PDF span origin; no alignment, translation, rescaling or changed default printer',
            'cases':[]}
    def save():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    def convert(source,folder):
        profile=folder/'profile';folder.mkdir()
        prefix=[str(args.executable.resolve())]if args.executable else[sys.executable,str(ROOT/'run_magic_converter.py')]
        done=subprocess.run(prefix+['convert',str(source),'--action','document:pdf','--output',str(folder/'output'),
              '--data-dir',str(profile),'--report',str(folder/'job.json'),'--json','--timeout','120'],capture_output=True,timeout=180,
              env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'conversion.log').write_bytes(done.stdout+done.stderr);job=json.loads((folder/'job.json').read_text(encoding='utf-8-sig'))
        if done.returncode or not job['ok']:raise ValueError(str(job))
        if not any(w.startswith("rhwp ") for w in job["jobs"][0].get("warnings", [])):
            raise ValueError("PDF was not rendered by rhwp")
        with fitz.open(job['jobs'][0]['outputs'][0])as pdf:
            spans=[s for b in pdf[0].get_text('dict')['blocks']if 'lines'in b for l in b['lines']for s in l['spans']]
            if not spans or 'Origin'not in ''.join(s['text']for s in spans):raise ValueError('Missing text control')
            return {'pages':len(pdf),'origin_pt':list(spans[0]['origin']),'fonts':sorted({s['font']for s in spans}),
                    'media_box_pt':list(pdf[0].mediabox),'crop_box_pt':list(pdf[0].cropbox),'rotation':pdf[0].rotation}
    save()
    try:
        for margin in(36,72,108):
            folder=out/str(margin);folder.mkdir();source=folder/'control.hwpx'
            write_hwpx(DocumentData([Paragraph([TextRun('Origin control ABC 123',font='Segoe UI')])],
                                   margins=(margin,72,margin,72)),source)
            original=sha(source);portable=convert(source,folder/'rhwp')
            if abs(portable['origin_pt'][0]-margin)>.1:raise ValueError('rhwp does not honor declared left margin')
            case={'declared_left_margin_pt':margin,'declared_top_margin_pt':margin,'input_sha256':original,'rhwp':portable}
            if sha(source)!=original:raise ValueError('Original control modified')
            report['cases'].append(case);save()
        report['status']='passed'
    except Exception as exc:report.update(status='failed',error=type(exc).__name__+': '+str(exc))
    save();print(json.dumps(report,ensure_ascii=True));return int(report['status']!='passed')

if __name__=='__main__':raise SystemExit(main())
