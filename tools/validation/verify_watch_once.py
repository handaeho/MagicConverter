"""Actual frozen mixed --to watch batch, restart, refusal and recovery."""
import argparse
import csv
import io
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha
from openpyxl import Workbook


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--executable',type=Path,required=True);args=parser.parse_args()
    root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    inbox=root/'inbox';inbox.mkdir();profile=root/'profile'
    source=inbox/'structured.json';source.write_text('[{"name":"data","value":42}]',encoding='utf-8')
    workbook=Workbook();workbook.active.append(['name','value']);workbook.active.append(['sheet',17])
    workbook.save(inbox/'table.xlsx')
    original={p.name:sha(p) for p in inbox.iterdir()}
    report={'status':'running','version':__version__,'executable_sha256':sha(args.executable),'cases':[]}
    def save():(root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    def run(label,count,ok):
        path=root/(label+'.json');env=os.environ.copy();env['PYTHONUTF8']='1'
        env['PATH']=str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32')
        done=subprocess.run([str(args.executable.resolve()),'watch',str(inbox),'--to','csv','--settle','.1',
             '--once','--data-dir',str(profile),'--report',str(path),'--json'],
             capture_output=True,timeout=90,env=env,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        (root/(label+'.log')).write_bytes(done.stdout+done.stderr)
        data=json.loads(path.read_text(encoding='utf-8-sig'))
        if done.returncode!=(0 if ok else 1) or data['ok']!=ok or len(data['jobs'])!=count:raise ValueError(str(data))
        report['cases'].append({'case':label,'status':'passed','expected_success':ok,'jobs':len(data['jobs'])});save()
        return data
    save()
    try:
        data=run('initial',2,True);values=[]
        for job in data['jobs']:
            output=Path(job['outputs'][0]);paths=[output] if output.is_file() else list(output.glob('*.csv'))
            for path in paths:
                values.extend(csv.DictReader(io.StringIO(path.read_text(encoding='utf-8-sig'))))
        if {(r['name'],r['value']) for r in values}!={('data','42'),('sheet','17')}:raise ValueError('Mixed batch content changed')
        run('restart',0,True)
        bad=inbox/'bad.json';bad.write_text('{invalid',encoding='utf-8')
        data=run('refusal',1,False)
        if any(j['outputs'] for j in data['jobs']):raise ValueError('Failed input published output')
        (inbox/'recovery.json').write_text('[{"name":"recovery","value":9}]',encoding='utf-8')
        run('recovery',1,True)
        if any(sha(inbox/name)!=digest for name,digest in original.items()):raise ValueError('Original input changed')
        report['status']='passed'
    except Exception as error:report.update(status='failed',error=str(error))
    save();print(json.dumps(report,indent=2));return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
