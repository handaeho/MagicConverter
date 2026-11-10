"""Windows application-owned process RSS samples, distinct from payload/commit."""
import argparse
import ctypes
from ctypes import wintypes
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import time
import zipfile

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT))
import pymupdf as fitz
import win32api
import win32event
import win32job
import win32process
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha
from magic_converter.core.jobs.process_runner import start_process,stop_process


def members(job):
    kernel=ctypes.WinDLL('kernel32',use_last_error=True)
    query=kernel.QueryInformationJobObject
    query.argtypes=[wintypes.HANDLE,ctypes.c_int,ctypes.c_void_p,wintypes.DWORD,ctypes.c_void_p]
    query.restype=wintypes.BOOL
    buffer=ctypes.create_string_buffer(8+1024*ctypes.sizeof(ctypes.c_size_t))
    if not query(int(job),3,buffer,len(buffer),None): raise ctypes.WinError(ctypes.get_last_error())
    count=ctypes.c_uint32.from_buffer(buffer,4).value
    if count>1024: raise RuntimeError('Process observation capacity exceeded')
    return list((ctypes.c_size_t*count).from_buffer(buffer,8))


def measure(command, folder, *, expected_ok, deadline=180, office_directory=None):
    folder.mkdir()
    started=time.monotonic();handles={};identities={};peak_rss={};peak_private={};peak_sum=0;samples=0
    from tools.validation.office_memory import OfficeMemory
    office=OfficeMemory(office_directory) if office_directory is not None else None
    peak_with_office=0;office_result=None
    with (folder/'stdout.log').open('wb') as output,(folder/'stderr.log').open('wb') as errors:
        launch_ns = time.perf_counter_ns()
        process,job=start_process(command,stdout=output,stderr=errors)
        try:
            while True:
                current_rss=0
                for pid in members(job):
                    if pid not in handles:
                        try:
                            handle=win32api.OpenProcess(0x100410,False,pid)
                            identities[pid]=str(win32process.GetProcessTimes(handle)['CreationTime'])
                            handles[pid]=handle
                        except Exception: continue
                    try:
                        info=win32process.GetProcessMemoryInfo(handles[pid])
                        rss=info['WorkingSetSize'];private=info.get('PrivateUsage',info['PagefileUsage'])
                        current_rss+=rss;peak_rss[pid]=max(peak_rss.get(pid,0),rss)
                        peak_private[pid]=max(peak_private.get(pid,0),private)
                    except Exception: pass  # An owned process can exit between enumeration and query.
                peak_sum=max(peak_sum,current_rss);samples+=1
                if office is not None:
                    external=office.sample(members(job))
                    peak_with_office=max(peak_with_office,current_rss+external)
                if process.poll() is not None: break
                if time.monotonic()-started>deadline: raise TimeoutError('Verifier deadline exceeded')
                time.sleep(.025)
            root_exit_ns = time.perf_counter_ns()
            process.wait(timeout=5)
            peak_job_commit=win32job.QueryInformationJobObject(job,win32job.JobObjectExtendedLimitInformation)['PeakJobMemoryUsed']
            accounting = win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAndIoAccountingInformation)
            # Job accounting and a process's wait handle can settle at slightly
            # different times during Windows/DLL teardown. Observe a bounded
            # grace interval before closing the outer guard; never use the
            # verifier's termination to make this assertion pass.
            settling_started=time.monotonic()
            initially_pending=[]
            while True:
                for pid in members(job):
                    if pid not in handles:
                        try:
                            handle=win32api.OpenProcess(0x100410,False,pid)
                            handles[pid]=handle
                            identities[pid]=str(win32process.GetProcessTimes(handle)['CreationTime'])
                        except Exception: continue
                lingering=[pid for pid,handle in handles.items() if win32event.WaitForSingleObject(handle,0)==258]
                if not initially_pending: initially_pending=lingering.copy()
                if not lingering or time.monotonic()-settling_started>=1: break
                time.sleep(.01)
            settling_seconds=round(time.monotonic()-settling_started,4)
            tree_exit_ns = time.perf_counter_ns()
            (folder/'process-observation.json').write_text(json.dumps({'root_pid':process.pid,
                'initially_pending_pids':initially_pending,'settling_seconds':settling_seconds,
                'lingering_before_verifier_cleanup':lingering,'owned_processes':identities},indent=2),encoding='utf-8')
            if lingering: raise RuntimeError(f'Owned PIDs still running before verifier cleanup: {lingering}')
            if office is not None:
                office_result=office.finish()
                (folder/'office-observation.json').write_text(json.dumps(office_result,indent=2),encoding='utf-8')
                if office_result['lingering_before_verifier_cleanup']:
                    raise RuntimeError('Owned Office server still running before verifier cleanup')
        finally:
            stop_process(process,job)
            for handle in handles.values(): handle.Close()
            if office is not None:
                try:
                    if office_result is None:
                        office_result=office.finish()
                        (folder/'office-observation.json').write_text(json.dumps(office_result,indent=2),encoding='utf-8')
                finally:office.close()
    result=json.loads((folder/'stdout.log').read_text(encoding='utf-8-sig'))
    if expected_ok is None:
        if type(result.get('ok')) is not bool:
            raise ValueError('Missing measured success/failure outcome')
        expected_ok = result['ok']
    if process.returncode!=(0 if expected_ok else 1) or result['ok']!=expected_ok: raise ValueError(str(result))
    paths=[Path(p) for job_result in result['jobs'] for p in job_result.get('outputs',[])]
    if not expected_ok and paths: raise ValueError('Rejected job published an output')
    return {'status':'passed','expected_success':expected_ok,'exit':process.returncode,
            'seconds':round(time.monotonic()-started,3),'sample_interval_seconds':.025,'samples':samples,
            'clock_ns': {'launch': launch_ns, 'root_exit_observed': root_exit_ns, 'tree_exit_observed': tree_exit_ns},
            'job_accounting': accounting,
            'sampled_tree_peak_rss_bytes':peak_sum,'job_peak_commit_bytes':peak_job_commit,
            'office_observation':office_result,'sampled_tree_and_external_office_peak_rss_bytes':peak_with_office if office is not None else None,
            'processes':[{'pid':pid,'created':identities[pid],'sampled_peak_rss_bytes':peak_rss.get(pid,0),
                          'sampled_peak_private_bytes':peak_private.get(pid,0)} for pid in identities],
            'exit_settling_seconds':settling_seconds,'initially_pending_pids':initially_pending,
            'lingering_owned_pids':lingering,'outputs':[{'path':str(p),'bytes':p.stat().st_size,'sha256':sha(p)} for p in paths],
            'jobs':result['jobs']}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--corpus',type=Path);parser.add_argument('--executable',type=Path)
    parser.add_argument('--rss-ceiling-mib',type=int,default=1024)
    args=parser.parse_args();root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
    if args.rss_ceiling_mib<=0: parser.error('rss-ceiling-mib must be positive')
    report={'status':'running','version':__version__,'cases':[],
            'executable_sha256':sha(args.executable) if args.executable else None,
            'measurement':'Sampled sum of owned Windows working sets; shared pages may be counted multiple times. Not an exact peak or a hard RSS limit.',
            'worker_commit_limit_bytes':2*1024**3,'limitations':['Windows Job memory limit is committed memory, not RSS. External Office memory is separate. Direct library APIs require caller supervision.']}
    report['benchmark_tree_rss_ceiling_bytes']=args.rss_ceiling_mib*1024**2
    def save(): (root/'report.json').write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding='utf-8')
    save()
    try:
        prefix=[str(args.executable.resolve())] if args.executable else [sys.executable,str(ROOT/'run_magic_converter.py')]
        fixtures=root/'inputs';fixtures.mkdir()
        if args.corpus:
            pdfs=sorted(args.corpus.resolve().glob('*.pdf'))
            if not pdfs: raise ValueError('No actual corpus PDFs')
        else:
            pdfs=[]
            for index in range(3):
                path=fixtures/f'control-{index}.pdf'
                with fitz.open() as pdf:
                    for page in range(20): pdf.new_page().insert_text((30,60),f'Input {index} page {page}')
                    pdf.save(path)
                pdfs.append(path)
        report['pdf_input_kind']='actual user corpus' if args.corpus else 'generated controls'
        binaries=[]
        for index in range(2):
            path=fixtures/f'random-{index}.bin';chunk=random.Random(740+index).randbytes(1024*1024)
            with path.open('wb') as stream:
                for _ in range(32): stream.write(chunk)
            binaries.append(path)
        normal=fixtures/'normal.csv'
        with normal.open('wb') as stream:
            stream.write(b'name,value\n');block=(b'row,'+b'x'*240+b'\n')*1000
            for _ in range(120): stream.write(block)
        excessive=fixtures/'many-rows.csv'
        with excessive.open('wb') as stream:
            stream.write(b'a,b\n')
            for _ in range(5000): stream.write(b'x,y\n'*1000)
        originals={str(p):sha(p) for p in [*pdfs,*binaries,normal,excessive]}
        report['inputs']=[{'path':p,'bytes':Path(p).stat().st_size,'sha256':h} for p,h in originals.items()];save()
        cases=[('pdf-merge',pdfs,['--action','tool:pdf_merge'],True),
               ('batch-zip',binaries,['--action','zip'],True),
               ('large-valid-csv',[normal],['--to','json'],True),
               ('excessive-csv-rows',[excessive],['--to','json'],False),
               ('deadline-zip',binaries,['--action','zip','--timeout','.3'],False)]
        recovery=fixtures/'recovery.json';recovery.write_text('{"x":42}',encoding='utf-8')
        cases.append(('recovery',[recovery],['--to','yaml'],True))
        for label,paths,flags,ok in cases:
            destination=root/(label+'-output')
            command=prefix+['convert',*[str(p) for p in paths],*flags,'--output',str(destination),
                            '--data-dir',str(root/'profile'),'--json']
            case=measure(command,root/label,expected_ok=ok)
            case['case']=label;report['cases'].append(case)
            if case['sampled_tree_peak_rss_bytes']>report['benchmark_tree_rss_ceiling_bytes']:
                case.update(status='failed',reason='benchmark RSS regression ceiling exceeded')
                raise ValueError('Benchmark tree RSS regression ceiling exceeded; this is not the application commit limit')
            if not ok and destination.exists() and list(destination.iterdir()): raise ValueError('Failed job left partial/reserved outputs')
            if label=='pdf-merge':
                expected=0
                for path in pdfs:
                    with fitz.open(path) as incoming: expected+=len(incoming)
                with fitz.open(case['outputs'][0]['path']) as pdf:
                    if len(pdf)!=expected: raise ValueError('Merged pages missing')
                    case['verified_pages']=len(pdf)
            if label=='batch-zip':
                with zipfile.ZipFile(case['outputs'][0]['path']) as archive:
                    if archive.testzip() is not None or len(archive.infolist())!=2: raise ValueError('ZIP content corrupted')
                    for path in binaries:
                        import hashlib
                        with archive.open(path.name) as stream:
                            if hashlib.file_digest(stream,'sha256').hexdigest()!=originals[str(path)]: raise ValueError('ZIP input bytes changed')
            if label=='large-valid-csv':
                value=json.loads(Path(case['outputs'][0]['path']).read_text(encoding='utf-8-sig'))
                if len(value)!=120000 or value[0]!=value[-1] or value[0]!={'name':'row','value':'x'*240}:
                    raise ValueError('Valid CSV output was truncated or changed')
                del value
            if label=='deadline-zip' and not case['jobs'][0]['timed_out']: raise ValueError('Deadline failure was not marked timed_out')
            save();print(json.dumps({'case':label,'rss_bytes':case['sampled_tree_peak_rss_bytes'],'status':'passed'}),flush=True)
        if any(sha(Path(p))!=h for p,h in originals.items()): raise ValueError('Original input changed')
        report['status']='passed'
    except Exception as error: report.update(status='failed',error=str(error))
    save();return 0 if report['status']=='passed' else 1


if __name__=='__main__': raise SystemExit(main())
