"""Read-only actual large Word/Excel outcomes under existing native limits.

Resource containment success and conversion success are separate report fields.
No failure, cancellation or timeout is relabelled as a successful conversion.
"""
import argparse,json,os,subprocess,sys,tempfile,shutil
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha
from tools.validation.verify_large_input_rss import measure
from tools.validation.verify_office_corpus_rss import metadata


def worker(exe,source,destination,folder,timeout):
    from magic_converter.core.engines.office_process import run_helper
    from magic_converter.core.engines.office_com import cleanup_owned_processes
    request,report=folder/'request.json',folder/'native-result.json'
    private=folder/'native-private-output';private.mkdir()
    native=private/'native.pdf'
    request.write_text(json.dumps({'source':str(source),'destination':str(native),'operation':'office_pdf'}),encoding='utf-8')
    ok=False;error=''
    try:
        code,errors=run_helper([str(exe),'--office-com',str(request),str(report)],report.with_suffix('.state.json'),timeout=timeout)
        result=json.loads(report.read_text(encoding='utf-8'))if report.exists()else{}
        ok=code==0 and result.get('ok')is True and native.is_file();error=result.get('error',errors)
        if ok:
            from magic_converter.core.engines.hwp_converter import validate_output
            validate_output(native)
            fd,name=tempfile.mkstemp(prefix='.validation-publish-',suffix='.pdf',dir=destination.parent);os.close(fd)
            temporary=Path(name)
            try:shutil.copyfile(native,temporary);os.replace(temporary,destination)
            finally:temporary.unlink(missing_ok=True)
    except Exception as e:error=type(e).__name__+': '+str(e)
    finally:cleanup_owned_processes(report.with_suffix('.processes.json'))
    artifacts=[{'name':p.name,'bytes':p.stat().st_size}for p in private.iterdir()if p.is_file()]
    print(json.dumps({'ok':ok,'jobs':[{'outputs':[str(destination)]if ok else[],'error':error,'route':'direct frozen COM',
        'publication_scope':'Validation caller stages internal protocol output; public CLI atomicity is checked separately',
        'private_native_artifacts':artifacts}]}))
    return 0 if ok else 1


def main():
    p=argparse.ArgumentParser();p.add_argument('--corpus',type=Path,required=True)
    p.add_argument('--executable',type=Path,required=True);p.add_argument('--output',type=Path,required=True)
    p.add_argument('--resume',action='store_true')
    a=p.parse_args();out=a.output.resolve();out.mkdir(parents=True,exist_ok=a.resume)
    paths=sorted(x.resolve()for x in a.corpus.iterdir()if x.is_file()and x.suffix.lower()in{'.docx','.xlsx'}and not x.name.startswith('~$'))
    report={'status':'running','version':__version__,'input_kind':'actual user-provided archive DOCX/XLSX',
        'executable_sha256':sha(a.executable),'inputs':[],'cases':[],
        'limitations':['25ms RSS samples may miss short peaks and double-count shared pages.',
            'Bounded resource/atomicity verification is distinct from successful full document conversion.',
            'Rendering an entire 667845-row worksheet is measured under a finite deadline, not promised to succeed.']}
    if a.resume:
        previous_report=json.loads((out/'report.json').read_text(encoding='utf-8'))
        if previous_report['status']!='failed'or previous_report['executable_sha256']!=report['executable_sha256']:raise ValueError('Cannot resume a different or completed measurement')
        if not previous_report.get('original_hashes_unchanged'):raise ValueError('Previous originals were not preserved')
        for item in previous_report['inputs']:
            if sha(Path(item['path']))!=item['sha256']:raise ValueError('Original changed before resume')
        report=previous_report;report.update(status='running',resumed_internal_protocol_adapter=True)
        report.pop('error',None)
        report['cases']=[c for c in report['cases']if not c['case'].endswith('direct-com')]
    def save():(out/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    scratch=None;before={k:os.environ.get(k)for k in('TMP','TEMP','PYTHONUTF8')}
    save()
    try:
        import pymupdf as fitz
        if {x.suffix.lower()for x in paths}!={'.docx','.xlsx'}:raise ValueError('Actual Word and Excel inputs required')
        report['inputs']=[{'path':str(x),'sha256':sha(x),'bytes':x.stat().st_size,**metadata(x)}for x in paths];save()
        parent=(ROOT/'.test-artifacts').resolve()
        scratch=Path(report['scratch_root'])if a.resume else Path(tempfile.mkdtemp(prefix='ul-',dir=parent))
        if scratch.parent!=parent:raise ValueError('Unexpected scratch parent')
        report['scratch_root']=str(scratch);os.environ['PYTHONUTF8']='1'
        for source in paths:
            for route,timeout in(('cli',60),('direct-com',60),('cli-deadline',1)):
                label=source.suffix[1:]+'-'+route
                if any(c['case']==label for c in report['cases']):continue
                folder=scratch/(label+'-staged-retry'if a.resume else label);folder.mkdir()
                os.environ['TMP']=os.environ['TEMP']=str(folder)
                destination=out/(label+'-output');destination.mkdir()
                if route.startswith('cli'):
                    command=[str(a.executable.resolve()),'convert',str(source),'--action','document:pdf','--output',str(destination),
                        '--data-dir',str(out/'profile'),'--timeout',str(timeout),'--json']
                else:
                    command=[sys.executable,'-B',str(Path(__file__).resolve()),'--worker',str(a.executable.resolve()),
                        str(source),str(destination/'native.pdf'),str(folder),str(timeout)]
                case=measure(command,out/label,expected_ok=False if timeout==1 else None,deadline=timeout+60,office_directory=folder)
                case.update(case=label,source=str(source),conversion_status='passed'if case['expected_success']else'failed',
                    resource_boundary_status='passed',deadline_seconds=timeout)
                servers=case['office_observation']['processes']
                if timeout>1 and not servers:raise ValueError('Native server not observed')
                for server in servers:
                    if server.get('native_commit_limit_bytes')!=2*1024**3 or server.get('budget_phase')!='before_process_resume':
                        raise ValueError('Native startup budget receipt missing')
                if case['expected_success']:
                    if len(case['outputs'])!=1:raise ValueError('Expected one complete PDF')
                    with fitz.open(case['outputs'][0]['path'])as pdf:
                        if pdf.is_encrypted or not len(pdf):raise ValueError('Invalid PDF')
                        case['pdf']={'pages':len(pdf),'text_characters':sum(len(page.get_text())for page in pdf)}
                elif any(destination.iterdir()):raise ValueError('Failed conversion published partial output')
                if sha(source)!=next(i['sha256']for i in report['inputs']if i['path']==str(source)):raise ValueError('Original changed')
                report['cases'].append(case);save();print(json.dumps({'case':label,'conversion':case['conversion_status'],
                    'boundary':case['resource_boundary_status'],'rss':case['sampled_tree_and_external_office_peak_rss_bytes']}),flush=True)
        report['status']='resource_boundaries_verified'
    except Exception as e:report.update(status='failed',error=type(e).__name__+': '+str(e))
    finally:
        report['original_hashes_unchanged']=all(Path(i['path']).is_file()and sha(Path(i['path']))==i['sha256']for i in report['inputs'])
        if not report['original_hashes_unchanged']:report['status']='failed'
        if scratch:
            report['scratch_evidence']=[]
            for path in scratch.rglob('*'):
                if path.is_file()and path.suffix.lower()in{'.json','.log'}:
                    target=out/'native-receipts'/path.relative_to(scratch);target.parent.mkdir(parents=True,exist_ok=True);shutil.copy2(path,target)
                    report['scratch_evidence'].append({'copy':str(target),'sha256':sha(target)})
            report['scratch_cleanup']='retained_for_identity_checked_stage_cleanup'
        for k,v in before.items():
            if v is None:os.environ.pop(k,None)
            else:os.environ[k]=v
        save()
    return int(report['status']!='resource_boundaries_verified')

if __name__=='__main__':
    if len(sys.argv)>1 and sys.argv[1]=='--worker':raise SystemExit(worker(*map(Path,sys.argv[2:6]),float(sys.argv[6])))
    raise SystemExit(main())
