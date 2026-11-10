"""Abrupt helper exit must release only its pre-budgeted native server."""
import argparse,json,os,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.diagnostics.visual_comparison import sha
from magic_converter.core.jobs.process_runner import start_process,stop_process


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--executable',type=Path)
    a=p.parse_args();root=a.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'executable_sha256':sha(a.executable) if a.executable else None,
        'cases':[],'scope':'Terminate only the validation-owned helper after its pre-resume native budget receipt; observe native exit before closing the outer verifier Job.',
        'limits':'Installed native servers only; explicit vendor registration repair is separate.'}
    def save():(root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    save()
    try:
        import win32api,win32event,win32process,win32job
        from tools.validation.windows_process_tree import owned_native_parent
        from docx import Document
        from openpyxl import Workbook
        from pptx import Presentation
        word=root/'control.docx';doc=Document();doc.add_paragraph('Must not open user input before owned bootstrap');doc.save(word)
        excel=root/'control.xlsx';book=Workbook();book.active.append(['Must not open user input']);book.save(excel);book.close()
        powerpoint=root/'control.pptx';deck=Presentation();deck.slides.add_slide(deck.slide_layouts[6]);deck.save(powerpoint)
        originals={str(path):sha(path) for path in (word,excel,powerpoint)}
        prefix=[str(a.executable.resolve())] if a.executable else [sys.executable,str(ROOT/'run_magic_converter.py')]
        for kind,source in [('word',word),('excel',excel),('powerpoint',powerpoint)]:
            folder=root/kind;folder.mkdir();output=folder/'output.pdf'
            request,result=folder/'request.json',folder/'result.json';ledger=result.with_suffix('.processes.json')
            pass
            process=job=handle=runtime=None
            try:
                with (folder/'stdout.log').open('wb') as log,(folder/'stderr.log').open('wb') as errors:
                    process,job=start_process([*prefix,'--office-com',str(request),str(result)],stdout=log,stderr=errors,memory_limit=2*1024**3)
                    started=time.monotonic();receipt=None
                    while time.monotonic()-started<45:
                        if ledger.exists():
                            try:
                                owned=json.loads(ledger.read_text(encoding='utf-8'))['owned']
                                receipt=next((x for x in owned if x.get('application')==kind and x.get('budget_phase')=='before_process_resume'),None)
                            except (OSError,ValueError,KeyError):pass
                        if receipt:break
                        if process.poll() is not None:raise ValueError('Helper exited before prelaunch receipt')
                        time.sleep(.01)
                    if receipt is None:raise TimeoutError('No prelaunch receipt')
                    handle=win32api.OpenProcess(0x100400,False,receipt['pid'])
                    if str(win32process.GetProcessTimes(handle)['CreationTime'])!=receipt['created']:raise ValueError('Native identity changed')
                    if win32event.WaitForSingleObject(handle,0)!=258:raise ValueError('Native server already exited')
                    # One-file PyInstaller's Popen root is the bootloader,
                    # while the native Job handle lives in its Python child.
                    # Kill the identity/Job-checked actual producer instead.
                    owner=owned_native_parent(receipt['pid'],process.pid)
                    runtime=win32api.OpenProcess(0x100401,False,owner)
                    if not win32job.IsProcessInJob(runtime,job):raise ValueError('Runtime owner outside verifier Job')
                    runtime_created=str(win32process.GetProcessTimes(runtime)['CreationTime'])
                    if output.exists():raise ValueError('Observation arrived after output; crash case not exercised')
                    win32api.TerminateProcess(runtime,1);win32event.WaitForSingleObject(runtime,10000);process.wait(timeout=10)
                    ended=win32event.WaitForSingleObject(handle,5000)==0
                    item={'application':kind,'status':'passed' if ended and not output.exists() else 'failed',
                        'receipt':receipt,'helper_exit':process.returncode,'native_exited_before_outer_job_cleanup':ended,
                        'runtime_owner_pid':owner,'runtime_owner_created':runtime_created,
                        'published_output':output.exists()}
                    report['cases'].append(item);save()
                    if item['status']!='passed':raise ValueError('Helper crash left owned native server or output')
            finally:
                if process is not None:stop_process(process,job)
                if handle is not None:handle.Close()
                if runtime is not None:runtime.Close()
        if any(sha(Path(path))!=h for path,h in originals.items()):raise ValueError('Input changed')
        report.update(status='passed',original_hashes=originals)
    except Exception as exc:report.update(status='failed',error=type(exc).__name__+': '+str(exc))
    save();print(json.dumps(report),flush=True);return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
