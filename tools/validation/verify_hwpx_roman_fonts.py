"""Verify searchable Roman numerals in rhwp PDF output."""
import argparse,json,os,subprocess,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
import pymupdf as fitz
from magic_converter import __version__
from magic_converter.core.documents.hwp_documents import DocumentData,Paragraph,TextRun,write_hwpx
from magic_converter.core.engines.rhwp_roman import available_replacements
from magic_converter.core.diagnostics.visual_comparison import sha

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path)
    args=parser.parse_args();out=args.output.resolve();out.mkdir(parents=True,exist_ok=False)
    source=out/'control.hwpx';paragraphs=[]
    for font in('굴림','휴먼명조','맑은 고딕','Segoe UI Symbol'):
        for bold in(False,True):paragraphs.append(Paragraph([TextRun(f'{font} {bold}: Ⅰ Ⅹ Ⅺ Ⅻ 가나다 ABC',font=font,size=14,bold=bold)]))
    write_hwpx(DocumentData(paragraphs),source);original=sha(source)
    report={'status':'running','version':__version__,'input_sha256':original,'executable_sha256':sha(args.executable)if args.executable else None,
            'cases':{}}
    def save():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        available=available_replacements()
        report['installed_controls']={name:'required'if name in available else'skipped: exact installed coverage not established'for name in('굴림','휴먼명조')}
        mode='rhwp';folder=out/mode;folder.mkdir();profile=folder/'profile'
        prefix=[str(args.executable.resolve())]if args.executable else[sys.executable,str(ROOT/'run_magic_converter.py')]
        done=subprocess.run(prefix+['convert',str(source),'--action','document:pdf','--output',str(folder/'output'),'--data-dir',str(profile),
            '--report',str(folder/'job.json'),'--json','--timeout','120'],capture_output=True,timeout=180,
            env={**os.environ,'PYTHONUTF8':'1'},creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (folder/'conversion.log').write_bytes(done.stdout+done.stderr);job=json.loads((folder/'job.json').read_text(encoding='utf-8-sig'))
        if done.returncode or not job['ok']:raise ValueError(str(job))
        rows=[]
        with fitz.open(job['jobs'][0]['outputs'][0])as pdf:
            if len(pdf)!=1:raise ValueError('Control pagination changed')
            for block in pdf[0].get_text('dict')['blocks']:
                for line in block.get('lines',[]):
                    spans=line['spans'];text=''.join(s['text']for s in spans)
                    rows.append({'text':text,'roman_faces':sorted({s['font']for s in spans if any(c in s['text']for c in'ⅪⅫ')}),
                                 'roman_spans':[{'text':s['text'],'font':s['font'],'origin_pt':s['origin'],'size_pt':s['size']}for s in spans if any(c in s['text']for c in'ⅪⅫ')]})
        if len(rows)!=8 or any(''.join(c for c in row['text']if not c.isspace()).count('ⅠⅩⅪⅫ')!=1 for row in rows):raise ValueError('Roman/control text lost')
        for index,name in((0,'굴림'),(2,'휴먼명조')):
            if name in available:
                if any(not row['roman_faces'] for row in rows[index:index+2]):raise ValueError('Missing Roman glyph font')
        report['cases'][mode]={'rows':rows,'warnings':job['jobs'][0].get('warnings',[]),'status':'passed'};save()
        if sha(source)!=original:raise ValueError('Original changed')
        report['status']='passed'
    except Exception as exc:report.update(status='failed',error=type(exc).__name__+': '+str(exc))
    save();print(json.dumps({'status':report['status'],'error':report.get('error')}));return int(report['status']!='passed')
if __name__=='__main__':raise SystemExit(main())
