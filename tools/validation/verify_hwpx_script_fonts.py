"""Real source/frozen conversion must retain explicit HWPX script faces."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.documents.hwp_documents import read_docx
from magic_converter.core.diagnostics.visual_comparison import sha


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',required=True,type=Path);parser.add_argument('--executable',type=Path);args=parser.parse_args()
    out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    source=ROOT/'tests/fixtures/hwp/script-faces.hwpx';original=sha(source)
    report={'status':'running','version':__version__,'input_sha256':original,'executable_sha256':sha(args.executable)if args.executable else None,
            'scope':'Model HWPX to DOCX declarations; does not certify native rendering/font files/12pt origin'}
    try:
        prefix=[str(args.executable.resolve())]if args.executable else[sys.executable,str(ROOT/'run_magic_converter.py')]
        done=subprocess.run(prefix+['convert',str(source),'--action','document:docx','--output',str(out/'output'),
              '--data-dir',str(out/'profile'),'--report',str(out/'job.json'),'--json','--timeout','60'],capture_output=True,timeout=120,
              env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (out/'conversion.log').write_bytes(done.stdout+done.stderr);job=json.loads((out/'job.json').read_text(encoding='utf-8-sig'))
        if done.returncode or not job['ok']:raise ValueError(str(job))
        data=read_docx(Path(job['jobs'][0]['outputs'][0]));actual=[(r.text,r.font)for r in data.blocks[0].runs]
        expected=[('Ab','Arial'),('가','Gulim'),('中','Batang'),('あ','Meiryo'),('·Ⅺ','Gulim')]
        if actual!=expected:raise ValueError('Script font declarations lost: '+str(actual))
        if sha(source)!=original:raise ValueError('Original changed')
        report.update(status='passed',declarations=actual,original_unchanged=True)
    except Exception as exc:report.update(status='failed',error=type(exc).__name__+': '+str(exc))
    (out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8');print(json.dumps(report,ensure_ascii=True));return int(report['status']!='passed')

if __name__=='__main__':raise SystemExit(main())
