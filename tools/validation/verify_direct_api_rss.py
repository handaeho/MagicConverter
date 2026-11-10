"""Direct PDF/ZIP API RSS trends, byte/page checks and deadline recovery."""
import argparse
from dataclasses import replace
import json
import os
from pathlib import Path
import random
import sys
import threading
import time
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha


def run_case(path):
    from magic_converter.core.conversion.converter import create_zip
    from magic_converter.core.conversion.builtin_converters import create_catalog
    from magic_converter.core.conversion.conversion_request import prepare_request
    from magic_converter.core.pdf.pdf_tools import merge_pdf
    from magic_converter.core.jobs.job_control import Control,current,JobTimedOut
    from magic_converter.core.settings.preferences import OutputRule
    item=json.loads(path.read_text(encoding='utf-8'));paths=tuple(Path(p) for p in item['paths'])
    rule=OutputRule('folder',item['output']);token=None
    if item.get('timeout') is not None:
        token=current.set(Control(threading.Event(),lambda _:None,time.monotonic()+item['timeout']))
    report={'ok':False,'jobs':[{'outputs':[]}]}
    try:
        if item['action']=='zip':result=create_zip(paths,rule=rule)
        else:
            request=prepare_request(paths,item['action'],create_catalog(),output=rule)
            # Preserve deliberate repeated inputs, which the low-level API has
            # always accepted, while deriving supported file routes normally.
            by_source={f.source:f for f in request.files}
            request=replace(request,files=tuple(by_source[p.resolve()] for p in paths))
            result=merge_pdf(request,lambda _:None)
        report.update(ok=True);report['jobs'][0]['outputs']=[str(result)]
    except Exception as error:
        report['jobs'][0].update(error=str(error),timed_out=isinstance(error,JobTimedOut))
    finally:
        if token is not None:current.reset(token)
    print(json.dumps(report));return 0 if report['ok'] else 1


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path)
    parser.add_argument('--corpus',type=Path);parser.add_argument('--case',type=Path)
    parser.add_argument('--rss-ceiling-mib',type=int,default=1536)
    args=parser.parse_args()
    if args.case:return run_case(args.case)
    if not args.output or args.rss_ceiling_mib<=0:parser.error('output and positive rss ceiling required')
    import pymupdf as fitz
    from tools.validation.verify_large_input_rss import measure
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False);inputs=root/'inputs';inputs.mkdir()
    report={'status':'running','version':__version__,'mode':'source direct Python APIs','cases':[],
            'benchmark_tree_rss_ceiling_bytes':args.rss_ceiling_mib*1024**2,
            'measurement':'25ms sampled sum of owned Windows working sets; shared pages can be counted twice and short peaks missed.',
            'limitations':['Worker 2 GiB commit differs from RSS. External Office is outside these PDF/ZIP operations. Other low-level encoders still require caller supervision.']}
    def save():(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        pdfs=sorted(args.corpus.resolve().glob('*.pdf')) if args.corpus else []
        if not pdfs:
            if args.corpus:raise ValueError('No corpus PDFs')
            for i in range(3):
                path=inputs/f'control-{i}.pdf'
                with fitz.open() as pdf:
                    for page in range(20):pdf.new_page().insert_text((20,30),f'{i}/{page}')
                    pdf.save(path)
                pdfs.append(path)
        binaries=[]
        for i in range(2):
            path=inputs/f'random-{i}.bin';chunk=random.Random(214+i).randbytes(1024**2)
            with path.open('wb') as stream:
                for _ in range(32):stream.write(chunk)
            binaries.append(path)
        originals={str(p):sha(p) for p in [*pdfs,*binaries]}
        report['inputs']=[{'path':p,'bytes':Path(p).stat().st_size,'sha256':h} for p,h in originals.items()]
        total_pages=0
        for path in pdfs:
            with fitz.open(path) as pdf:total_pages+=len(pdf)
        cases=[(f'pdf-{factor}x',pdfs*factor,'tool:pdf_merge',None,True) for factor in (1,2,3)]
        cases += [('zip-64mib',binaries,'zip',None,True),('zip-256mib',binaries*4,'zip',None,True),
                  ('deadline',binaries*4,'zip',.3,False),('recovery',binaries[:1],'zip',None,True)]
        for label,paths,action,timeout,ok in cases:
            destination=root/(label+'-output');request=root/(label+'-request.json')
            request.write_text(json.dumps({'paths':[str(p) for p in paths],'output':str(destination),
                                          'action':action,'timeout':timeout}),encoding='utf-8')
            case=measure([sys.executable,str(Path(__file__).resolve()),'--case',str(request)],root/label,expected_ok=ok,deadline=240)
            case.update(label=label,input_bytes=sum(p.stat().st_size for p in paths));report['cases'].append(case);save()
            if case['sampled_tree_peak_rss_bytes']>report['benchmark_tree_rss_ceiling_bytes']:raise ValueError('Explicit direct API benchmark RSS ceiling exceeded')
            if ok:
                output=Path(case['outputs'][0]['path'])
                if action=='zip':
                    with zipfile.ZipFile(output) as archive:
                        if len(archive.infolist())!=len(paths):raise ValueError('ZIP member count differs')
                        for member,source in zip(archive.infolist(),paths):
                            import hashlib
                            with archive.open(member) as stream:actual=hashlib.file_digest(stream,'sha256').hexdigest()
                            if actual!=originals[str(source)]:raise ValueError('ZIP member bytes differ')
                else:
                    with fitz.open(output) as pdf:
                        case['output_pages']=len(pdf)
                        if len(pdf)!=total_pages*int(label[4]):raise ValueError('Merged page count differs')
            elif not case['jobs'][0].get('timed_out') or (destination.exists() and any(destination.iterdir())):
                raise ValueError('Deadline did not reject atomically')
            save()
        if any(sha(Path(p))!=h for p,h in originals.items()):raise ValueError('Original inputs changed')
        report.update(status='passed',source_pages_per_set=total_pages)
    except Exception as error:report.update(status='failed',error=str(error))
    save();print(json.dumps({k:v for k,v in report.items() if k not in {'inputs','cases'}},ensure_ascii=False,indent=2))
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
