"""Actual validation-owned empty PowerPoint survives another conversion.

Never quit a server found in the pre-test PID snapshot. Bootstrap and reused
server memory remain outside the converter's native Job, recorded explicitly.
"""
import argparse,gc,json,os,subprocess,sys,time
from pathlib import Path
ROOT=Path(__file__).resolve().parents[2];sys.path.insert(0,str(ROOT))
from magic_converter import __version__

def main():
 parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True)
 parser.add_argument('--executable',type=Path);parser.add_argument('--expect-quit',action='store_true');args=parser.parse_args()
 root=args.output.resolve();root.mkdir(parents=True,exist_ok=False)
 report={'status':'running','version':__version__,'expected_old_defect':args.expect_quit,
  'limits':'Validation-owned pre-existing control server is never assigned to the converter Job. A new bounded process is tested after the control closes.'}
 def save():(root/'report.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
 save();app=None;handle=None;owned=False
 import pythoncom,win32api,win32process,win32event,win32com.client
 from magic_converter.core.engines.office_com import _powerpoint_hwnd
 pythoncom.CoInitialize()
 try:
  from pptx import Presentation
  import pymupdf as fitz
  before=set(win32process.EnumProcesses());app=win32com.client.DispatchEx('PowerPoint.Application')
  if app.Presentations.Count:raise RuntimeError('Pre-existing presentation: control not isolated; no changes made')
  blank=app.Presentations.Add(WithWindow=False)
  try:
   from magic_converter.core.engines.office_activation import powerpoint_pids
   fresh_pids=powerpoint_pids()-before
   if len(fresh_pids)!=1:raise RuntimeError('Control has no unique new PowerPoint PID; ownership not assumed')
   hwnd=_powerpoint_hwnd(app,next(iter(fresh_pids)));_,pid=win32process.GetWindowThreadProcessId(hwnd)
   owned=pid not in before
   handle=win32api.OpenProcess(0x0400|0x00100000|(0x0001 if owned else 0),False,pid)
   created=str(win32process.GetProcessTimes(handle)['CreationTime'])
   owned=pid not in before
  finally:
   blank.Close();blank=None
  if not owned:raise RuntimeError('Factory reused a pre-test server; no ownership assumed')
  report['control']={'pid':pid,'created':created,'presentations_before':app.Presentations.Count}
  deck=Presentation();deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text='Empty reused server preservation'
  source=root/'control.pptx';deck.save(source);job=root/'job.json'
  prefix=[str(args.executable.resolve())] if args.executable else [sys.executable,str(ROOT/'run_magic_converter.py')]
  def direct_native(destination,label):
   request=root/(label+'-request.json');result=root/(label+'-result.json')
   request.write_text(json.dumps({'operation':'office_pdf','source':str(source),'destination':str(destination)}),encoding='utf-8')
   done=subprocess.run(prefix+['--office-com',str(request),str(result)],capture_output=True,timeout=90,
    env={**os.environ,'PYTHONUTF8':'1'},creationflags=subprocess.CREATE_NO_WINDOW)
   (root/(label+'.log')).write_bytes(done.stdout+done.stderr)
   return done.returncode,json.loads(result.read_text(encoding='utf-8'))
  def control_state():
   return {'count':app.Presentations.Count,'visible':bool(app.Visible),'caption':str(app.Caption),
    'alerts':app.DisplayAlerts,'security':app.AutomationSecurity}
  state_before=control_state()
  process=subprocess.run(prefix+['convert',str(source),'--action','document:pdf','--output',str(root/'converted'),
   '--data-dir',str(root/'profile'),'--timeout','60','--report',str(job),'--json'],capture_output=True,timeout=120,
   env={**os.environ,'PYTHONUTF8':'1'},creationflags=subprocess.CREATE_NO_WINDOW)
  (root/'conversion.log').write_bytes(process.stdout+process.stderr)
  result=json.loads(job.read_text(encoding='utf-8-sig'))
  if process.returncode==0 and result['ok']:
   target=Path(result['jobs'][0]['outputs'][0])
   with fitz.open(target) as pdf:assert 'Empty reused server preservation' in pdf[0].get_text()
   report['conversion']='passed'
  else:
   errors=[str(error) for item in result.get('jobs',[]) for error in item.get('errors',[])]
   assert not args.expect_quit and any('기존 Office 서버' in error for error in errors),result
   assert not any(item.get('outputs') for item in result.get('jobs',[])),result
   report['conversion']='refused_existing_server'
   report['refusal_errors']=errors
  if not args.expect_quit:
   # The normal CLI can legitimately use Word's document model. Exercise the
   # actual PowerPoint API too; its refusal cannot be filled by a Word result.
   direct_target=root/'refused-native.pdf';code,direct_result=direct_native(direct_target,'direct-refusal')
   if not code or direct_result.get('ok') or '기존 Office 서버' not in direct_result.get('error','') or direct_target.exists():
    raise RuntimeError('Direct PowerPoint did not refuse the existing server')
   report['direct_native_refusal']='passed';report['direct_refusal_error']=direct_result['error']
   state_after=control_state()
   if state_before!=state_after:raise RuntimeError('Existing PowerPoint settings changed')
   report.update(state_before=state_before,state_after=state_after)
  survived=win32event.WaitForSingleObject(handle,0)==win32event.WAIT_TIMEOUT
  report['server_survived']=survived
  if args.expect_quit:
   report['status']='not_reproduced' if survived else 'old_defect_reproduced'
  else:
   assert survived,'Existing empty server was quit'
   assert app.Presentations.Count==0,'Conversion left another document in the existing server'
  if report['status']!='not_reproduced':report['status']='old_defect_reproduced' if args.expect_quit else 'passed'
 except Exception as error:report.update(status='failed',error=type(error).__name__+': '+str(error))
 finally:
  if owned and handle is not None and win32event.WaitForSingleObject(handle,0)==win32event.WAIT_TIMEOUT:
   try:app.Quit()
   except Exception:pass
   app=None;gc.collect()
   deadline=time.monotonic()+10
   while time.monotonic()<deadline and win32event.WaitForSingleObject(handle,100)==win32event.WAIT_TIMEOUT:pass
   if win32event.WaitForSingleObject(handle,0)==win32event.WAIT_TIMEOUT:
    assert str(win32process.GetProcessTimes(handle)['CreationTime'])==created
    win32api.TerminateProcess(handle,1);win32event.WaitForSingleObject(handle,5000)
    report['owned_control_forced_after_grace']=True
   report['owned_control_cleaned']=win32event.WaitForSingleObject(handle,0)==win32event.WAIT_OBJECT_0
  else:report['owned_control_cleaned']=owned
  if handle is not None:handle.Close()
  app=None;pythoncom.CoUninitialize();save()
 if report.get('status')=='passed' and report.get('conversion')=='refused_existing_server' and report.get('owned_control_cleaned'):
  # Once our own empty server is gone, ordinary owned conversion must work.
  # A refusal cannot conceal a regression of the normal fresh-server path.
  try:
   fresh=root/'fresh.json'
   result=subprocess.run(prefix+['convert',str(source),'--action','document:pdf','--output',str(root/'fresh'),
    '--data-dir',str(root/'fresh-profile'),'--timeout','60','--report',str(fresh),'--json'],capture_output=True,timeout=120,
    env={**os.environ,'PYTHONUTF8':'1'},creationflags=subprocess.CREATE_NO_WINDOW)
   (root/'fresh.log').write_bytes(result.stdout+result.stderr)
   payload=json.loads(fresh.read_text(encoding='utf-8-sig'));assert result.returncode==0 and payload['ok'],payload
   with fitz.open(payload['jobs'][0]['outputs'][0]) as pdf:assert 'Empty reused server preservation' in pdf[0].get_text()
   report['fresh_server_control']='passed'
  except Exception as error:report.update(status='failed',fresh_server_error=str(error))
  save()
 if report.get('status')=='passed' and report.get('owned_control_cleaned') and not args.expect_quit:
  try:
   target=root/'fresh-native.pdf';code,payload=direct_native(target,'direct-fresh')
   if code or not payload.get('ok'):raise RuntimeError(str(payload))
   with fitz.open(target) as pdf:
    if 'Empty reused server preservation' not in pdf[0].get_text():raise RuntimeError('Fresh native output missing')
   receipts=json.loads((root/'direct-fresh-result.processes.json').read_text(encoding='utf-8'))['owned']
   if len(receipts)!=1 or receipts[0].get('application')!='powerpoint' or receipts[0].get('budget_phase')!='before_process_resume':
    raise RuntimeError('Fresh PowerPoint missing pre-resume budget')
   report.update(fresh_direct_native='passed',fresh_native_receipts=receipts)
  except Exception as error:report.update(status='failed',fresh_direct_native_error=str(error))
  save()
 print(json.dumps(report),flush=True)
 return 0 if report['status'] in {'passed','old_defect_reproduced'} and report['owned_control_cleaned'] else 1

if __name__=='__main__':raise SystemExit(main())
