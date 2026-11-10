"""Actual source/frozen combined jobs, GIF frames, font EPUB and refusals."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import zipfile
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pymupdf
from PIL import Image, ImageSequence
from magic_converter import __version__
from tests.regression.test_epub_protection import fixture
from tools.audits.audit_followups import epubcheck


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--jar',type=Path,required=True)
    parser.add_argument('--executable',type=Path)
    args=parser.parse_args(); folder=args.output.resolve();folder.mkdir(parents=True,exist_ok=False)
    prefix=[str(args.executable.resolve())] if args.executable else [sys.executable,str(ROOT/'run_magic_converter.py')]
    report={'status':'running','version':__version__,'mode':'frozen' if args.executable else 'source','cases':[]}
    if args.executable:
        with args.executable.open('rb') as stream: report['executable_sha256']=hashlib.file_digest(stream,'sha256').hexdigest()
    report_path=folder/'report.json'
    def save():report_path.write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    env=os.environ.copy();env['PYTHONUTF8']='1'
    if args.executable:
        env['PATH']=str(Path(os.environ.get('SystemRoot','C:/Windows'))/'System32')
        for key in ('PYTHONPATH','PYTHONHOME','VIRTUAL_ENV','MAGIC_CONVERTER_FFMPEG','QT_PLUGIN_PATH','QT_QPA_PLATFORM_PLUGIN_PATH'):env.pop(key,None)
    def convert(paths,action,label,success=True):
        output=folder/label;receipt=folder/(label+'.json')
        done=subprocess.run(prefix+['convert',*map(str,paths),'--action',action,'--output',str(output),
            '--data-dir',str(folder/'profile'),'--report',str(receipt),'--json'],env=env,capture_output=True,timeout=120,
            creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
        data=json.loads(receipt.read_text(encoding='utf-8-sig'))
        if done.returncode != (0 if success else 1) or data['ok'] != success:raise ValueError(str(data))
        if not success:
            if output.exists() and list(output.iterdir()):raise ValueError('Failed job published output')
            report['cases'].append({'case':label,'result':'refused_without_publication'});save();return
        result=Path(data['jobs'][0]['outputs'][0]);report['cases'].append({'case':label,'result':'passed','output':str(result)});save();return result
    try:
        pdfs=[]
        for index in range(2):
            path=folder/f'input-{index}.pdf'
            with pymupdf.open() as doc:doc.new_page().insert_text((20,40),f'Page {index}');doc.save(path)
            pdfs.append(path)
        merged=convert(pdfs[::-1],'tool:pdf_merge','merged')
        with pymupdf.open(merged) as doc:
            if [p.get_text().strip() for p in doc]!=['Page 1','Page 0']:raise ValueError('Merge order changed')
        zipped=convert(pdfs,'zip','zipped')
        with zipfile.ZipFile(zipped) as archive:
            if [archive.read(p.name) for p in pdfs]!=[p.read_bytes() for p in pdfs]:raise ValueError('ZIP bytes changed')
        bad=folder/'bad.pdf';bad.write_bytes(b'not PDF');convert([pdfs[0],bad],'tool:pdf_merge','bad-merge',False)
        gif=folder/'animated.gif'
        frames=[Image.new('RGBA',(16,12),'red'),Image.new('RGBA',(16,12),'blue')]
        frames[0].save(gif,save_all=True,append_images=frames[1:],duration=[40,80],loop=2)
        for target in ('png','webp'):
            output=convert([gif],'image:'+target,'gif-'+target)
            with Image.open(output) as image:
                image.load();timing=[]
                for frame in ImageSequence.Iterator(image):frame.load();timing.append(frame.info['duration'])
                if image.n_frames!=2 or image.info['loop']!=3 or timing!=[40,80]:raise ValueError('GIF timing changed')
        bad=folder/'bad.gif';bad.write_bytes(b'not GIF');convert([bad],'image:png','bad-gif',False)
        source=fixture(folder)
        for label,path in [('font-input',source),('font-output',convert([source],'document:epub','font-output'))]:
            checked=epubcheck(path,folder,args.jar.resolve(),'java')
            if checked['exit'] or any(m['severity'] in {'FATAL','ERROR','WARNING'} for m in checked['messages']):raise ValueError(str(checked))
            report['cases'].append({'case':label,'epubcheck':checked});save()
        with zipfile.ZipFile(path) as archive:
            if 'META-INF/encryption.xml' in archive.namelist() or any(n.endswith('.ttf') for n in archive.namelist()):raise ValueError('Font was recovered or copied')
        for label,settings in [('sinf',{'marker':'META-INF/sinf.xml'}),('unknown',{'algorithm':'urn:unknown'}),
                               ('chapter',{'uri':'EPUB/chapter0.xhtml'}),('missing',{'uri':'EPUB/missing.ttf'})]:
            bad_folder=folder/label;bad_folder.mkdir();bad=fixture(bad_folder,**settings)
            convert([bad],'document:epub','refuse-'+label,False)
        report['status']='passed'
    except Exception as error:report.update(status='failed',error=str(error))
    save();print(json.dumps({'status':report['status'],'report':str(report_path)}));return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
