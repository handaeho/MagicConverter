"""Direct registered data/image/PDF APIs: RSS, bounded rejection and recovery."""
import argparse
from contextlib import redirect_stdout
import json
import os
from pathlib import Path
import sys
import threading
import time
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha


def run_case(path):
    from magic_converter.core.conversion.builtin_converters import create_catalog
    from magic_converter.core.conversion.conversion_execution import execute_file
    from magic_converter.core.conversion.conversion_request import prepare_request
    from magic_converter.core.jobs.job_control import current,Control,JobTimedOut
    from magic_converter.core.settings.preferences import OutputRule
    item=json.loads(path.read_text());result={'ok':False,'jobs':[{'outputs':[]}]};token=None
    if item.get('timeout') is not None:
        token=current.set(Control(threading.Event(),lambda _:None,time.monotonic()+item['timeout']))
    try:
        with redirect_stdout(sys.stderr):
            request=prepare_request([Path(item['source'])],item['action'],create_catalog(),output=OutputRule('folder',item['output']))
            output=execute_file(request,request.files[0],None,lambda _:None,lambda _:None)
        result['ok']=True;result['jobs'][0]['outputs']=[str(output)]
    except Exception as error:result['jobs'][0].update(error=str(error),timed_out=isinstance(error,JobTimedOut))
    finally:
        if token is not None:current.reset(token)
    print(json.dumps(result));return 0 if result['ok'] else 1


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path);parser.add_argument('--case',type=Path)
    args=parser.parse_args()
    if args.case:return run_case(args.case)
    if not args.output:parser.error('--output required')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'cases':[],
        'scope':'Direct registered encoders; raw read_data/convert_image/model functions remain caller-supervised.',
        'benchmark_tree_rss_ceiling_bytes':1024**3}
    def save():(root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    save()
    try:
        from PIL import Image
        import pymupdf as fitz
        from pptx import Presentation
        from tools.validation.verify_large_input_rss import measure
        inputs=root/'inputs';inputs.mkdir()
        csv=inputs/'normal.csv'
        with csv.open('wb') as stream:
            stream.write(b'name,value\n')
            for _ in range(120):stream.write((b'row,'+b'x'*240+b'\n')*1000)
        excess=inputs/'excess.csv'
        with excess.open('wb') as stream:
            stream.write(b'a,b\n')
            for _ in range(1000):stream.write(b'x,y\n'*1000)
        image=inputs/'large.png'
        with Image.new('RGB',(5000,4000),'red') as picture:picture.save(image)
        pdf=inputs/'text.pdf'
        with fitz.open() as doc:
            for i in range(20):doc.new_page().insert_text((30,60),f'Direct API PDF page {i+1}')
            doc.save(pdf)
        cases=[(csv,'data:json','data-normal',None,True),(excess,'data:json','data-excess',None,False),
               (image,'image:jpg','image-normal',None,True),(pdf,'pptx_editable','pdf-normal',None,True),
               (csv,'data:json','deadline',.3,False),(csv,'data:json','recovery',None,True)]
        originals={str(p):sha(p) for p,*_ in cases}
        for source,action,label,timeout,ok in cases:
            output=root/(label+'-output');request=root/(label+'-request.json')
            request.write_text(json.dumps({'source':str(source),'action':action,'output':str(output),'timeout':timeout}))
            item=measure([sys.executable,str(Path(__file__).resolve()),'--case',str(request)],root/label,expected_ok=ok)
            item['case']=label;report['cases'].append(item);save()
            if item['sampled_tree_peak_rss_bytes']>report['benchmark_tree_rss_ceiling_bytes']:raise ValueError('Benchmark RSS ceiling exceeded')
            if ok:
                result=Path(item['outputs'][0]['path'])
                if action=='data:json':
                    value=json.loads(result.read_text(encoding='utf-8-sig'))
                    if len(value)!=120000 or value[0]!=value[-1] or value[0]!={'name':'row','value':'x'*240}:raise ValueError('Data differs')
                elif action=='image:jpg':
                    with Image.open(result) as picture:
                        if picture.size!=(5000,4000):raise ValueError('Image dimensions differ')
                elif len(Presentation(result).slides)!=20:raise ValueError('PPTX page count differs')
            elif output.exists() and any(output.iterdir()):raise ValueError('Rejected job published output')
            if label=='deadline' and not item['jobs'][0].get('timed_out'):raise ValueError('Deadline was not recognized')
            save();print(json.dumps({'case':label,'rss':item['sampled_tree_peak_rss_bytes'],'status':'passed'}),flush=True)
        if any(sha(Path(p))!=h for p,h in originals.items()):raise ValueError('Input changed')
        report.update(status='passed',original_hashes=originals)
    except Exception as error:report.update(status='failed',error=str(error))
    save();return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
