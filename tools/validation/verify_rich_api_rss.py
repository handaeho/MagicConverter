"""Source/frozen rich API boundaries: values, bounded wire, deadline and RSS."""
import argparse
from contextlib import nullcontext
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha


def run_case(path):
    from magic_converter.core.documents import rich_hwp as rich
    from magic_converter.core.documents.hwp_documents import DocumentData,Paragraph,TextRun,document_html
    from magic_converter.core.jobs import specialized_worker,raw_api
    from magic_converter.core.jobs.job_control import Control,current,JobTimedOut
    item=json.loads(path.read_text(encoding='utf-8'))
    temporary=Path(item['temporary']);temporary.mkdir()
    os.environ['TMP']=os.environ['TEMP']=str(temporary);tempfile.tempdir=str(temporary)
    source,output=Path(item['source']),Path(item['output'])
    result={'ok':False,'jobs':[{'outputs':[]}]}
    token=current.set(Control(threading.Event(),lambda _:None,time.monotonic()+item['timeout'])) if item.get('timeout') else None
    replacement=patch.object(specialized_worker,'worker_command',side_effect=lambda a,b:[item['executable'],'--specialized-convert',str(a),str(b)]) if item.get('executable') else nullcontext()
    try:
        with replacement:
            kind=item['kind']
            if kind=='wire-excess':
                data=DocumentData([Paragraph([TextRun('x'*10000)])])
                with patch.object(raw_api,'MAX_WIRE_BYTES',4096):rich.document_html_rich(data)
                raise AssertionError('Oversized wire model was accepted')
            reader={'.docx':rich.read_docx_rich,'.hwpx':rich.read_hwpx_rich,'.hwp':rich.read_hwp_rich}[source.suffix.lower()]
            model=reader(source)
            if 'expected_text_sha256' in item:
                import hashlib
                if hashlib.sha256(model.text().encode('utf-8')).hexdigest()!=item['expected_text_sha256']:
                    raise ValueError('Rich model text differs')
            if kind=='html':
                html=rich.document_html_rich(model)
                if document_html(model)!=html or 'Rich paragraph 1999 &lt;text&gt; &amp; data' not in html:
                    raise ValueError('HTML escaping/value differs')
                output.write_text(html,encoding='utf-8')
            elif kind=='writer':
                rich.write_docx_rich(model,output)
                if rich.read_docx_rich(output).text()!=model.text():raise ValueError('Rich writer differs')
            else:
                output.write_text(json.dumps({'paragraphs':len(model.blocks),'characters':len(model.text()),
                    'objects':rich.object_counts(model)},ensure_ascii=True),encoding='utf-8')
        result['ok']=True;result['jobs'][0]['outputs']=[str(output)]
    except Exception as error:result['jobs'][0].update(error=str(error),timed_out=isinstance(error,JobTimedOut),error_type=type(error).__name__)
    finally:
        if token is not None:current.reset(token)
    print(json.dumps(result,ensure_ascii=True),flush=True)
    return 0 if result['ok'] else 1


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);parser.add_argument('--executable',type=Path)
    parser.add_argument('--corpus',type=Path);parser.add_argument('--case',type=Path)
    args=parser.parse_args()
    if args.case:return run_case(args.case)
    if not args.output:parser.error('--output required')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    if args.corpus and root.is_relative_to(args.corpus.resolve()):raise ValueError('Output must be outside original corpus')
    report={'status':'running','version':__version__,'cases':[],'executable_sha256':sha(args.executable) if args.executable else None,
        'worker_commit_limit_bytes':2*1024**3,'sample_interval_seconds':.025,'benchmark_tree_rss_ceiling_bytes':2300*1024**2,
        'note':'Measured owned process tree RSS is not the worker commit cap. HTML and rich functions share the facade serializer/worker; model methods remain in-process.',
        'supplied_hwp':'not supplied' if not args.corpus else 'pending'}
    def save():(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        import hashlib
        from docx import Document
        from magic_converter.core.documents.hwp_documents import read_docx,write_hwpx
        from tools.validation.verify_large_input_rss import measure
        inputs=root/'inputs';inputs.mkdir();docx=inputs/'document.docx';doc=Document()
        for index in range(2000):doc.add_paragraph(f'Rich paragraph {index} <text> & data')
        doc.save(docx);model=read_docx(docx);hwpx=inputs/'document.hwpx';write_hwpx(model,hwpx)
        text_sha=hashlib.sha256(model.text().encode('utf-8')).hexdigest()
        cases=[(docx,'reader','docx',None,True,text_sha),(hwpx,'reader','hwpx',None,True,text_sha),
            (docx,'html','html',None,True,text_sha),(docx,'writer','writer',None,True,text_sha),
            (docx,'wire-excess','wire-excess',None,False,None),(docx,'reader','deadline',.3,False,text_sha),
            (docx,'reader','recovery',None,True,text_sha)]
        if args.corpus:
            supplied=sorted(args.corpus.glob('*.hwp'))
            if not supplied:raise ValueError('No supplied HWP corpus')
            for index,path in enumerate(supplied):cases.append((path,'reader',f'hwp-{index}',None,True,None))
            report['supplied_hwp']=len(supplied)
        originals={str(p.resolve()):sha(p) for p,*_ in cases}
        for source,kind,label,timeout,expected,text in cases:
            output=root/(label+('.docx' if kind=='writer' else '.html' if kind=='html' else '.json'))
            request=root/(label+'-request.json')
            values={'source':str(source.resolve()),'kind':kind,'output':str(output),'timeout':timeout,
                'temporary':str(root/(label+'-temp')),'executable':str(args.executable.resolve()) if args.executable else None}
            if text is not None:values['expected_text_sha256']=text
            request.write_text(json.dumps(values,ensure_ascii=True),encoding='utf-8')
            item=measure([sys.executable,'-B',str(Path(__file__).resolve()),'--case',str(request)],root/label,expected_ok=expected)
            item['case']=label;report['cases'].append(item);save()
            if item['sampled_tree_peak_rss_bytes']>report['benchmark_tree_rss_ceiling_bytes']:raise ValueError('Benchmark RSS ceiling exceeded')
            if not expected and output.exists():raise ValueError('Failed call published output')
            if label=='deadline' and not item['jobs'][0].get('timed_out'):raise ValueError('Deadline not recognized')
            if label=='wire-excess' and item['jobs'][0].get('error_type')!='ValueError':raise ValueError('Transfer budget did not refuse the model')
            print(json.dumps({'case':label,'status':'passed','rss':item['sampled_tree_peak_rss_bytes']}),flush=True)
        if any(sha(Path(p))!=value for p,value in originals.items()):raise ValueError('Original input changed')
        report.update(status='passed',original_hashes=originals)
    except Exception as error:report.update(status='failed',error=str(error))
    save();return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
