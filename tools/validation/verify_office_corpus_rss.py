"""Read-only user OOXML corpus: frozen CLI and direct frozen COM RSS evidence."""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile
import zipfile
import xml.etree.ElementTree as ET

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha
from tools.validation.verify_large_input_rss import measure


def worker(executable,source,destination,folder):
    """Validation-only supervisor; conversion code executes in the frozen EXE."""
    from magic_converter.core.engines.office_process import run_helper
    from magic_converter.core.engines.office_com import cleanup_owned_processes
    request,report=folder/'request.json',folder/'native-result.json'
    request.write_text(json.dumps({'source':str(source),'destination':str(destination),
        'operation':'office_pdf'},ensure_ascii=False),encoding='utf-8')
    ok=False;error=''
    try:
        prefix=[sys.executable,str(executable)] if executable.suffix.lower()=='.py' else [str(executable)]
        code,errors=run_helper([*prefix,'--office-com',str(request),str(report)],
            report.with_suffix('.state.json'),timeout=180)
        result=json.loads(report.read_text(encoding='utf-8')) if report.exists() else {}
        ok=code==0 and result.get('ok') is True and destination.is_file()
        error=result.get('error',errors)
    except Exception as exc:
        error=type(exc).__name__+': '+str(exc)
    finally:
        cleanup_owned_processes(report.with_suffix('.processes.json'))
    print(json.dumps({'ok':ok,'jobs':[{'outputs':[str(destination)] if ok else [],
        'error':error,'route':('direct source' if executable.suffix.lower()=='.py' else 'direct frozen')+' --office-com; no document-model fallback'}]},ensure_ascii=False))
    return 0 if ok else 1


def metadata(path):
    with zipfile.ZipFile(path) as z:
        if z.testzip():raise ValueError('Invalid OOXML CRC')
        names=z.namelist()
        data={'media_files':sum('/media/' in n and not n.endswith('/') for n in names),
            'vba_parts':sum(n.lower().endswith('vbaproject.bin') for n in names)}
        if path.suffix.lower()=='.pptx':
            slides=[n for n in names if n.startswith('ppt/slides/slide') and n.endswith('.xml') and '/_rels/' not in n]
            data['slides']=len(slides)
            data['visible_slides']=sum(ET.fromstring(z.read(n)).get('show','1')!='0' for n in slides)
        elif path.suffix.lower()=='.xlsx':
            tree=ET.fromstring(z.read('xl/workbook.xml'))
            data['sheets']=len(tree.findall('.//{*}sheet'))
        else:
            data['paragraphs']=len(ET.fromstring(z.read('word/document.xml')).findall('.//{*}p'))
        return data


def main():
    p=argparse.ArgumentParser();p.add_argument('--corpus',type=Path,required=True)
    p.add_argument('--executable',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--rss-ceiling-mib',type=int,default=1536)
    a=p.parse_args();root=a.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'executable_sha256':sha(a.executable),
        'input_kind':'actual user-provided Desktop OOXML','inputs':[],'cases':[],
        'limitations':['Read-only structural/resource checks, not pixel-perfect rendering certification.',
            '25ms working-set samples can miss short peaks and double-count shared pages.',
            'Word/Excel/PowerPoint launch under the helper aggregate commit limit and a separate 2 GiB startup Job.',
            'DOCX/XLSX corpus sizes are reported exactly; large PPTX does not certify large DOCX/XLSX.']}
    def save():(root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    save()
    previous={k:os.environ.get(k) for k in ('TMP','TEMP','PYTHONUTF8')}
    scratch_root=None
    try:
        import pymupdf as fitz
        paths=sorted(x.resolve() for x in a.corpus.iterdir() if x.is_file() and x.suffix.lower() in {'.docx','.xlsx','.pptx'} and not x.name.startswith('~$'))
        if not {'.docx','.xlsx','.pptx'}.issubset({x.suffix.lower() for x in paths}):raise ValueError('DOCX/XLSX/PPTX controls required')
        originals={str(x):sha(x) for x in paths}
        report['inputs']=[{'path':str(x),'bytes':x.stat().st_size,'sha256':originals[str(x)],**metadata(x)} for x in paths];save()
        # Frozen bootloader extraction includes long third-party notice names.
        # Keep TEMP independent of the caller's (possibly deep) report folder.
        scratch_parent=ROOT/'.test-artifacts';scratch_parent.mkdir(exist_ok=True)
        if scratch_parent.resolve()!=ROOT/'.test-artifacts':raise ValueError('Unexpected scratch parent target')
        scratch_root=Path(tempfile.mkdtemp(prefix='oc-',dir=scratch_parent))
        report.update(scratch_root=str(scratch_root),scratch_policy='shallow owned workspace TEMP; native receipts copied before cleanup')
        os.environ['PYTHONUTF8']='1'
        for index,source in enumerate(paths):
            for route in ('cli','direct-com'):
                label=f'{index+1:02d}-{source.suffix[1:]}-{route}'
                scratch=scratch_root/label;scratch.mkdir()
                os.environ['TMP']=os.environ['TEMP']=str(scratch)
                destination=root/(label+'-output');destination.mkdir()
                if route=='cli':
                    command=[str(a.executable.resolve()),'convert',str(source),'--action','document:pdf',
                        '--output',str(destination),'--data-dir',str(root/'profile'),'--timeout','180','--json']
                else:
                    command=[sys.executable,'-B',str(Path(__file__).resolve()),'--worker',
                        str(a.executable.resolve()),str(source),str(destination/'native.pdf'),str(scratch)]
                case=measure(command,root/label,expected_ok=True,deadline=240,office_directory=scratch)
                case.update(case=label,source=str(source),route=route,input_bytes=source.stat().st_size)
                report['cases'].append(case);save()
                if case['sampled_tree_and_external_office_peak_rss_bytes']>a.rss_ceiling_mib*1024**2:raise ValueError('Benchmark RSS ceiling exceeded')
                servers=case['office_observation']['processes']
                if not servers or any(s.get('native_commit_limit_bytes')!=2*1024**3 for s in servers):raise ValueError('No native budget receipt')
                if route=='direct-com':
                    expected={'.docx':'WINWORD.EXE','.xlsx':'EXCEL.EXE','.pptx':'POWERPNT.EXE'}[source.suffix.lower()]
                    if not any(Path(s.get('process_image') or '').name.upper()==expected for s in servers):raise ValueError('Wrong or unobserved native Office application')
                for server in servers:
                    if Path(server.get('process_image') or '').name.upper() in {'WINWORD.EXE','EXCEL.EXE','POWERPNT.EXE'}:
                        if server.get('budget_phase')!='before_process_resume' or server.get('activation')!='direct_launch_rot':
                            raise ValueError('Office activation escaped its pre-resume budget')
                if len(case['outputs'])!=1:raise ValueError('Expected one PDF')
                with fitz.open(case['outputs'][0]['path']) as pdf:
                    if pdf.is_encrypted or not len(pdf):raise ValueError('Unreadable/empty PDF')
                    case['pdf']={'pages':len(pdf),'text_characters':sum(len(page.get_text()) for page in pdf),
                        'image_occurrences':sum(len(page.get_images()) for page in pdf),'producer':pdf.metadata.get('producer')}
                    if route=='direct-com' and source.suffix.lower()=='.pptx' and len(pdf)!=report['inputs'][index]['visible_slides']:raise ValueError('Native slide/page count differs')
                if sha(source)!=originals[str(source)]:raise ValueError('Original changed')
                save();print(json.dumps({'case':label,'status':'passed','rss_bytes':case['sampled_tree_and_external_office_peak_rss_bytes']},ensure_ascii=False),flush=True)
        report['original_hashes_unchanged']=all(sha(Path(x))==h for x,h in originals.items())
        if not report['original_hashes_unchanged']:raise ValueError('Input changed')
        report['status']='passed'
    except Exception as exc:report.update(status='failed',error=type(exc).__name__+': '+str(exc))
    finally:
        if report['inputs']:
            report['original_hashes_unchanged']=all(Path(x['path']).is_file() and sha(Path(x['path']))==x['sha256'] for x in report['inputs'])
            if not report['original_hashes_unchanged']:report.update(status='failed',error='Original input changed')
        if scratch_root is not None:
            try:
                report['scratch_evidence']=[]
                for folder in scratch_root.iterdir():
                    if not folder.is_dir():continue
                    evidence=root/(folder.name+'-temp');evidence.mkdir(exist_ok=True)
                    for item in folder.iterdir():
                        if item.is_file() and item.suffix.lower() in {'.json','.log'}:
                            target=evidence/item.name;shutil.copy2(item,target)
                            report['scratch_evidence'].append({'runtime_path':str(item),'copy':str(target),'sha256':sha(target)})
                if report['status']=='passed':
                    resolved=scratch_root.resolve()
                    if (not resolved.is_relative_to(ROOT) or resolved.parent!=(ROOT/'.test-artifacts').resolve()
                            or not resolved.name.startswith('oc-')):
                        raise ValueError('Unexpected owned scratch path')
                    if any(case.get('lingering_owned_pids') for case in report['cases']):
                        raise ValueError('Owned scratch is still in use')
                    shutil.rmtree(resolved)
                    report['scratch_cleanup']='passed'
                else:report['scratch_cleanup']='retained_after_failure'
            except Exception as exc:
                report.update(status='failed',scratch_cleanup='failed',scratch_cleanup_error=type(exc).__name__+': '+str(exc))
        for key,value in previous.items():
            if value is None:os.environ.pop(key,None)
            else:os.environ[key]=value
    save();return 0 if report['status']=='passed' else 1


if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker':raise SystemExit(worker(*map(Path,sys.argv[2:])))
    raise SystemExit(main())
