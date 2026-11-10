"""Actual public raw facades with source or final frozen parser workers."""
import argparse
import json
import os
from pathlib import Path
import sys
import threading
import tempfile
import time

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha


def run_case(path):
    from unittest.mock import patch
    from contextlib import nullcontext
    from magic_converter.core.jobs import specialized_worker
    from magic_converter.core.jobs.job_control import current, Control, JobTimedOut
    from magic_converter.core.specialized.data_converter import read_data
    from magic_converter.core.conversion.converter import convert_image
    from magic_converter.core.documents.hwp_documents import read_docx, write_hwpx, read_hwpx
    item = json.loads(path.read_text())
    temporary = Path(item['temporary']); temporary.mkdir()
    os.environ['TMP'] = os.environ['TEMP'] = str(temporary)
    tempfile.tempdir = str(temporary)
    source, output = Path(item['source']), Path(item['output'])
    result = {'ok':False, 'jobs':[{'outputs':[]}]}
    token = current.set(Control(threading.Event(), lambda _:None,
                        time.monotonic()+item['timeout'])) if item.get('timeout') else None
    replacement = (patch.object(specialized_worker, 'worker_command',
        side_effect=lambda a,b:[item['executable'],'--specialized-convert',str(a),str(b)])
        if item.get('executable') else nullcontext())
    try:
        with replacement:
            if item['kind'] == 'data':
                rows = read_data(source, 'csv')
                if len(rows) != 120000 or rows[0] != rows[-1] or rows[0] != {'name':'row','value':'x'*240}:
                    raise ValueError('Raw data differs')
                output.write_text(json.dumps({'rows':len(rows), 'first':rows[0], 'last':rows[-1]}))
            elif item['kind'] in {'image', 'svg'}:
                convert_image(source, output, '.png' if item['kind']=='svg' else '.jpg')
                from PIL import Image
                with Image.open(output) as image:
                    expected = (320,80) if item['kind']=='svg' else (5000,4000)
                    if image.size != expected:raise ValueError('Image dimensions differ')
                    if item['kind']=='svg' and not image.getchannel('A').getbbox():raise ValueError('SVG text is blank')
            else:
                model = read_docx(source)
                write_hwpx(model, output)
                if read_hwpx(output).text() != model.text():raise ValueError('Raw model differs')
        result.update(ok=True)
        result['jobs'][0]['outputs'] = [str(output)]
    except Exception as error:
        result['jobs'][0].update(error=str(error), timed_out=isinstance(error,JobTimedOut))
    finally:
        if token is not None:current.reset(token)
    print(json.dumps(result), flush=True)
    return 0 if result['ok'] else 1


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path)
    parser.add_argument('--executable',type=Path)
    parser.add_argument('--case',type=Path)
    args=parser.parse_args()
    if args.case:return run_case(args.case)
    if not args.output:parser.error('--output required')
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'cases':[],
        'executable_sha256':sha(args.executable) if args.executable else None,
        'worker_commit_limit_bytes':2*1024**3,'benchmark_tree_rss_ceiling_bytes':2300*1024**2,
        'note':'25ms sampled owned process RSS; parent wire transfer is byte/node bounded, not subject to the worker commit cap. External Office is not started.'}
    def save():(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        from PIL import Image
        from docx import Document
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
        svg=inputs/'text.svg'
        svg.write_text('<svg xmlns="http://www.w3.org/2000/svg" width="320" height="80">'
            '<text x="10" y="40" font-size="24">Normal SVG text</text></svg>',encoding='utf-8')
        docx=inputs/'document.docx';document=Document()
        for index in range(2000):document.add_paragraph(f'Raw model paragraph {index+1}')
        document.save(docx)
        cases=[(csv,'data','data-normal',None,True),(excess,'data','data-excess',None,False),
            (image,'image','image-normal',None,True),(svg,'svg','svg-normal',None,True),(docx,'model','model-normal',None,True),
            (csv,'data','deadline',.3,False),(csv,'data','recovery',None,True)]
        originals={str(p):sha(p) for p,*_ in cases}
        for source,kind,label,timeout,ok in cases:
            output=root/(label+{'image':'.jpg','svg':'.png','model':'.hwpx','data':'.json'}[kind])
            request=root/(label+'-request.json')
            request.write_text(json.dumps({'source':str(source),'kind':kind,'output':str(output),'timeout':timeout,
                'temporary':str(root/(label+'-temp')),
                'executable':str(args.executable.resolve()) if args.executable else None}),encoding='utf-8')
            item=measure([sys.executable,'-B',str(Path(__file__).resolve()),'--case',str(request)],root/label,expected_ok=ok)
            item['case']=label;report['cases'].append(item);save()
            if item['sampled_tree_peak_rss_bytes']>report['benchmark_tree_rss_ceiling_bytes']:
                raise ValueError('Benchmark RSS ceiling exceeded')
            if not ok and output.exists():raise ValueError('Failed raw call published output')
            if label=='deadline' and not item['jobs'][0].get('timed_out'):raise ValueError('Deadline was not recognized')
            print(json.dumps({'case':label,'status':'passed','rss':item['sampled_tree_peak_rss_bytes']}),flush=True)
        if any(sha(Path(p))!=digest for p,digest in originals.items()):raise ValueError('Original changed')
        report.update(status='passed',original_hashes=originals)
    except Exception as error:report.update(status='failed',error=str(error))
    save();return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
