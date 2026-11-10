"""New bounded instances must preserve validation-owned existing documents."""
import argparse,gc,json,os,sys,traceback
from contextlib import ExitStack,suppress
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__
from magic_converter.core.engines.office_com import ProcessLedger,application
from magic_converter.core.diagnostics.visual_comparison import sha
from tools.validation.verify_large_input_rss import measure


def main():
    p=argparse.ArgumentParser();p.add_argument('--output',type=Path,required=True);p.add_argument('--executable',type=Path)
    a=p.parse_args();root=a.output.resolve();root.mkdir(parents=True,exist_ok=False)
    report={'status':'running','version':__version__,'executable_sha256':sha(a.executable) if a.executable else None,
        'cases':[],'scope':'Existing servers/documents created by this verifier only; original user servers are never attached or closed.'}
    def save():(root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
    save();previous={k:os.environ.get(k) for k in ('TMP','TEMP')};com_ready=False;app=document=None
    try:
        import pythoncom
        pythoncom.CoInitialize();com_ready=True
        import pymupdf as fitz
        import win32api,win32event
        from docx import Document
        from openpyxl import Workbook
        word=root/'input.docx';doc=Document();doc.add_paragraph('Fresh Word native output');doc.save(word)
        excel=root/'input.xlsx';book=Workbook();book.active.append(['Fresh Excel native output']);book.active.column_dimensions['A'].width=60;book.save(excel);book.close()
        originals={str(path):sha(path) for path in (word,excel)}
        executable=a.executable.resolve() if a.executable else ROOT/'run_magic_converter.py'
        for kind,source,expected in [('word',word,'Fresh Word native output'),('excel',excel,'Fresh Excel native output')]:
            report['current_stage']=kind+': launch control';save()
            ledger=ProcessLedger(root/(kind+'-control.processes.json'));document=handle=None
            try:
                with application(kind,ledger) as app,ExitStack() as cleanup:
                    report['current_stage']=kind+': create control document';save()
                    document=app.Documents.Add(Visible=False) if kind=='word' else app.Workbooks.Add()
                    def close_created():
                        with suppress(Exception):document.Close(False)
                    cleanup.callback(close_created)
                    sentinel='Validation existing '+kind+' document'
                    if kind=='word':document.Content.Text=sentinel
                    else:document.Worksheets.Item(1).Range('A1').Value=sentinel
                    document.Saved=True
                    report['current_stage']=kind+': configure control';save()
                    app.DisplayAlerts=-1 if kind=='word' else 1
                    identity=ledger.owned[0];handle=win32api.OpenProcess(0x100000,False,identity['pid'])
                    def state():
                        return {'count':int(getattr(app,'Documents' if kind=='word' else 'Workbooks').Count),
                            'visible':bool(app.Visible),'alerts':app.DisplayAlerts,'security':app.AutomationSecurity,
                            'saved':bool(document.Saved),
                            'text':document.Content.Text if kind=='word' else document.Worksheets.Item(1).Range('A1').Value}
                    before=state();scratch=root/(kind+'-temp');scratch.mkdir();os.environ['TMP']=os.environ['TEMP']=str(scratch)
                    report['current_stage']=kind+': measure new converter';save()
                    target=root/(kind+'.pdf')
                    command=[sys.executable,'-B',str(ROOT/'tools/validation/verify_office_corpus_rss.py'),'--worker',
                        str(executable),str(source),str(target),str(scratch)]
                    measured=measure(command,root/(kind+'-measurement'),expected_ok=True,deadline=240,office_directory=scratch)
                    report['current_stage']=kind+': inspect existing state';save()
                    after=state();survived=win32event.WaitForSingleObject(handle,0)==258
                    if before!=after or not survived:raise ValueError('Existing validation document/server changed')
                    servers=measured['office_observation']['processes']
                    if not servers or any(s['pid']==identity['pid'] for s in servers):raise ValueError('Converter attached the existing control')
                    if not all(s.get('budget_phase')=='before_process_resume' for s in servers):raise ValueError('Missing pre-resume budget')
                    with fitz.open(target) as pdf:
                        if expected not in ''.join(page.get_text() for page in pdf):raise ValueError('Fresh native output missing')
                    report['cases'].append({'application':kind,'status':'passed','existing_control':identity,
                        'state_before':before,'state_after':after,'server_survived':survived,'measurement':measured})
                    document.Close(False);document=None;save()
            finally:
                # application() owns only the generated control instance.
                if handle is not None:handle.Close()
                ledger.budget.close()
                for key,value in previous.items():
                    if value is None:os.environ.pop(key,None)
                    else:os.environ[key]=value
        if any(sha(Path(path))!=h for path,h in originals.items()):raise ValueError('Input changed')
        report.update(status='passed',original_hashes=originals)
    except Exception as exc:report.update(status='failed',error=type(exc).__name__+': '+str(exc),traceback=traceback.format_exc())
    finally:
        # COM proxies/type-info must be released in the same live apartment;
        # restarting the STA between Word and Excel leaves stale type-info.
        app=document=None;gc.collect()
        if com_ready:pythoncom.CoUninitialize()
    save();print(json.dumps({'status':report['status'],'cases':len(report['cases']),'error':report.get('error')}),flush=True)
    return 0 if report['status']=='passed' else 1


if __name__=='__main__':raise SystemExit(main())
