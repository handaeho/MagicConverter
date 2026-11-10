"""Office activation must not escape its allocation budget before user input."""
import json
import os
from pathlib import Path
import tempfile
import sys
import threading
import time
import unittest
from unittest.mock import patch,MagicMock

from magic_converter.core.engines.office_com import ProcessLedger,application,com_apartment
from magic_converter.core.engines.office_engine import find_office
from magic_converter.core.jobs.job_control import Control,current,JobCancelled,JobTimedOut


class PrivateRecentTests(unittest.TestCase):
    def test_unavailable_user_recent_paths_are_preserved_and_only_private_entry_is_removed(self):
        from magic_converter.core.engines.office_activation import _remove_private_recent
        with tempfile.TemporaryDirectory()as directory:
            private=Path(directory)/'bootstrap.xlsx';private.write_bytes(b'private')
            entries=[]
            for parent,name in [(str(Path('//unavailable/share')),'user.xlsx'),
                                (str(Path('//unavailable/share')),'bootstrap.xlsx'),
                                (str(private.parent),'bootstrap.xlsx'),
                                (str(private.parent/'other'),'bootstrap.xlsx')]:
                item=MagicMock();item.Path=parent;item.Name=name;entries.append(item)
            app=MagicMock();app.RecentFiles.Count=len(entries)
            app.RecentFiles.Item.side_effect=lambda index:entries[index-1]
            original=Path.resolve;attempted=[]
            def resolve(candidate,*args,**kwargs):
                attempted.append(candidate)
                if str(candidate).startswith(('//','\\\\')):
                    raise OSError('network path unavailable')
                return original(candidate,*args,**kwargs)
            with patch.object(Path,'resolve',resolve):
                _remove_private_recent(app,private)
            entries[2].Delete.assert_called_once_with()
            for index in (0,1,3):entries[index].Delete.assert_not_called()
            self.assertFalse(any(candidate.name=='user.xlsx'for candidate in attempted))


@unittest.skipUnless(os.name=='nt' and find_office('Word') and find_office('Excel'),'Desktop Word and Excel required')
class NativePrelaunchTests(unittest.TestCase):
    def test_powerpoint_is_budgeted_without_scm_activation(self):
        for kind,available in [('powerpoint',find_office('PowerPoint'))]:
            if not available:continue
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory,com_apartment():
                ledger=ProcessLedger(Path(directory)/'report.processes.json')
                try:
                    with patch('win32com.client.DispatchEx',side_effect=AssertionError('Unbounded SCM activation')):
                        with application(kind,ledger) as app:
                            self.assertTrue(ledger.is_owned(app))
                            self.assertEqual(ledger.owned[0]['budget_phase'],'before_process_resume')
                            self.assertEqual(ledger.owned[0]['commit_limit_bytes'],2*1024**3)
                finally:ledger.budget.close()

    def test_word_and_excel_are_budgeted_before_activation_without_scm_or_add(self):
        for kind,collection in [('word','Documents'),('excel','Workbooks')]:
            with self.subTest(kind=kind),tempfile.TemporaryDirectory() as directory,com_apartment():
                ledger=ProcessLedger(Path(directory)/'report.processes.json')
                try:
                    with patch('win32com.client.DispatchEx',side_effect=AssertionError('Unbounded SCM activation')):
                        with application(kind,ledger) as app:
                            self.assertEqual(getattr(app,collection).Count,0)
                            self.assertTrue(ledger.is_owned(app))
                            records=json.loads(ledger.path.read_text(encoding='utf-8'))['owned']
                            self.assertEqual(len(records),1)
                            self.assertEqual(records[0]['budget_phase'],'before_process_resume')
                            self.assertEqual(records[0]['activation'],'direct_launch_rot')
                            self.assertEqual(records[0]['commit_limit_bytes'],2*1024**3)
                finally:ledger.budget.close()

    def test_cleanup_status_write_failure_still_closes_the_prelaunch_job(self):
        import win32api,win32event
        with tempfile.TemporaryDirectory() as directory,com_apartment():
            ledger=ProcessLedger(Path(directory)/'report.processes.json');original=ledger.mark;handle=None
            def mark(stage,detail=''):
                if stage=='cleanup':raise OSError('status disk failure')
                return original(stage,detail)
            try:
                with patch.object(ledger,'mark',side_effect=mark):
                    with self.assertRaisesRegex(OSError,'status disk failure'),application('word',ledger):
                        handle=win32api.OpenProcess(0x100000,False,ledger.owned[0]['pid'])
                self.assertEqual(ledger.launched,{})
                self.assertEqual(win32event.WaitForSingleObject(handle,1000),0)
            finally:
                if handle is not None:handle.Close()
                ledger.budget.close()


@unittest.skipUnless(os.name=='nt','Windows Job/ROT boundaries')
class PrelaunchFailureTests(unittest.TestCase):
    def test_excel_bootstrap_uses_automation_without_recovery_or_autostart(self):
        from magic_converter.core.engines.office_activation import _command
        with patch('magic_converter.core.engines.office_engine.find_office',return_value='C:/Office/EXCEL.EXE'):
            path=Path('C:/private/generated.xlsx');command=_command('excel',path)
        self.assertEqual(command,['C:/Office/EXCEL.EXE','/x','/e','/automation',str(path)])
        self.assertNotIn('/s',command)

    def test_powerpoint_snapshot_and_errors_refuse_before_any_launch(self):
        from magic_converter.core.engines import office_activation as activation
        import win32api
        with tempfile.TemporaryDirectory() as directory:
            ledger=ProcessLedger(Path(directory)/'report.processes.json')
            for records in [{777:{'parent':1,'image':'POWERPNT.EXE'}},OSError('snapshot unavailable')]:
                with self.subTest(records=str(records)),patch('magic_converter.core.jobs.windows_processes.process_snapshot',
                        **({'side_effect':records} if isinstance(records,Exception) else {'return_value':records})),patch(
                        'win32api.OpenProcess',side_effect=win32api.error(5,'OpenProcess','No VM access')),patch.object(activation,'start_process') as start:
                    with self.assertRaises((RuntimeError,OSError)):activation._refuse_existing_powerpoint()
                start.assert_not_called()

    def test_existing_powerpoint_refused_before_launch_or_com_lookup(self):
        from magic_converter.core.engines import office_activation as activation
        with tempfile.TemporaryDirectory() as directory:
            ledger=ProcessLedger(Path(directory)/'report.processes.json')
            with patch.object(activation,'powerpoint_pids',return_value={os.getpid()}),patch.object(
                    activation,'start_process') as start,patch.object(activation,'_running_application') as lookup:
                with self.assertRaisesRegex(RuntimeError,'기존 Office'),activation.bounded_application('powerpoint',ledger):self.fail('Reused server')
            start.assert_not_called();lookup.assert_not_called();self.assertEqual(ledger.owned,[])

    def test_powerpoint_missing_rot_cancel_and_foreign_pid_fail_closed(self):
        from magic_converter.core.engines import office_activation as activation
        for kind in ['powerpoint']:
            for mode in ['timeout','cancel','foreign']:
                with self.subTest(kind=kind,mode=mode),tempfile.TemporaryDirectory() as directory:
                    ledger=ProcessLedger(Path(directory)/'report.processes.json');app=MagicMock()
                    event=threading.Event();token=current.set(Control(event,lambda _:None))
                    def pending(*args):
                        if mode=='cancel':event.set()
                        return app if mode=='foreign' else None
                    try:
                        with self.thin(ledger),patch.object(activation,'_refuse_existing_powerpoint'),patch.object(
                                activation,'_running_application',side_effect=pending),patch.object(
                                activation,'STARTUP_SECONDS',.15),patch('magic_converter.core.engines.office_com.application_hwnd',return_value=123),patch(
                                'win32process.GetWindowThreadProcessId',return_value=(1,os.getpid())):
                            expected=JobCancelled if mode=='cancel' else JobTimedOut if mode=='timeout' else RuntimeError
                            with self.assertRaises(expected),activation.bounded_application(kind,ledger):self.fail('Unverified COM')
                        app.Quit.assert_not_called();app.Clear.assert_not_called();self.assert_stopped(ledger)
                    finally:current.reset(token)

    def test_read_only_powerpoint_frame_requires_unique_owned_process(self):
        from magic_converter.core.engines.office_com import _powerpoint_hwnd
        app=MagicMock();app.HWND.side_effect=RuntimeError('Unavailable');app.Caption='Original caption'
        with patch('magic_converter.core.engines.office_activation.powerpoint_pids',return_value={123}),patch(
                'win32gui.EnumWindows',side_effect=lambda visit,_:visit(42,None)),patch('win32gui.GetClassName',return_value='PPTFrameClass'),patch(
                'win32gui.GetWindowText',return_value='Original caption'),patch('win32process.GetWindowThreadProcessId',return_value=(1,123)):
            self.assertEqual(_powerpoint_hwnd(app,123),42)
        with patch('magic_converter.core.engines.office_activation.powerpoint_pids',return_value={123,456}),patch('win32gui.EnumWindows') as windows:
            self.assertEqual(_powerpoint_hwnd(app,123),0)
        windows.assert_not_called();self.assertEqual(app.Caption,'Original caption')

    def thin(self,ledger,kind='word'):
        from magic_converter.core.engines import office_activation as activation
        return patch.object(activation,'_command',return_value=[sys.executable,'-c','import time;time.sleep(30)'])

    def assert_stopped(self,ledger):
        import win32api,win32event,win32process
        self.assertTrue(ledger.owned)
        self.assertFalse(ledger.launched)
        for item in ledger.owned:
            try:handle=win32api.OpenProcess(0x100400,False,item['pid'])
            except win32api.error:continue
            try:
                if str(win32process.GetProcessTimes(handle)['CreationTime'])!=item['created']:continue
                self.assertEqual(win32event.WaitForSingleObject(handle,1000),0,item)
            finally:handle.Close()

    def test_missing_rot_registration_is_bounded_and_stops_own_server(self):
        from magic_converter.core.engines import office_activation as activation
        with tempfile.TemporaryDirectory() as directory:
            ledger=ProcessLedger(Path(directory)/'report.processes.json')
            with self.thin(ledger),patch.object(activation,'_running_document',return_value=None),patch.object(activation,'STARTUP_SECONDS',.2):
                started=time.monotonic()
                with self.assertRaises(JobTimedOut),activation.bounded_application('word',ledger):self.fail('Unbound input')
            self.assertLess(time.monotonic()-started,3)
            self.assert_stopped(ledger)

    def test_cancel_while_waiting_for_rot_stops_owned_server(self):
        from magic_converter.core.engines import office_activation as activation
        event=threading.Event();token=current.set(Control(event,lambda _:None))
        def pending(path):event.set();return None
        try:
            with tempfile.TemporaryDirectory() as directory:
                ledger=ProcessLedger(Path(directory)/'report.processes.json')
                with self.thin(ledger),patch.object(activation,'_running_document',side_effect=pending):
                    with self.assertRaises(JobCancelled),activation.bounded_application('word',ledger):self.fail('Unbound input')
                self.assert_stopped(ledger)
        finally:current.reset(token)

    def test_wrong_rot_path_or_pid_never_closes_or_changes_unowned_object(self):
        from magic_converter.core.engines import office_activation as activation
        for mismatch in ('path','pid'):
            with self.subTest(mismatch=mismatch),tempfile.TemporaryDirectory() as directory:
                ledger=ProcessLedger(Path(directory)/'report.processes.json')
                document=MagicMock();app=document.Application;app.Visible=True
                def bootstrap(folder,kind):
                    path=folder/'bootstrap.docx'
                    document.FullName=str(path if mismatch=='pid' else folder/'foreign.docx')
                    return path
                with self.thin(ledger),patch.object(activation,'_bootstrap',side_effect=bootstrap),patch.object(
                        activation,'_running_document',return_value=document),patch('win32process.GetWindowThreadProcessId',return_value=(1,os.getpid())):
                    with self.assertRaisesRegex(RuntimeError,'문서|서버'),activation.bounded_application('word',ledger):self.fail('Foreign object yielded')
                document.Close.assert_not_called();app.Quit.assert_not_called();self.assertIs(app.Visible,True)
                self.assert_stopped(ledger)

    def test_job_setup_failure_never_looks_up_rot_or_activates_scm(self):
        from magic_converter.core.engines import office_activation as activation
        with tempfile.TemporaryDirectory() as directory:
            ledger=ProcessLedger(Path(directory)/'report.processes.json')
            with self.thin(ledger),patch.object(activation,'start_process',side_effect=OSError('assignment refused')),patch.object(
                    activation,'_running_document') as lookup,patch('win32com.client.DispatchEx') as scm:
                with self.assertRaisesRegex(OSError,'assignment refused'),activation.bounded_application('word',ledger):self.fail('No Job')
            lookup.assert_not_called();scm.assert_not_called();self.assertEqual(ledger.owned,[])

    def test_limit_values_without_enforcement_flags_are_not_a_receipt(self):
        with tempfile.TemporaryDirectory() as directory:
            ledger=ProcessLedger(Path(directory)/'report.processes.json')
            process=MagicMock(pid=-1)
            limits={'BasicLimitInformation':{'LimitFlags':0},'ProcessMemoryLimit':2*1024**3,'JobMemoryLimit':2*1024**3}
            with patch('win32job.QueryInformationJobObject',return_value=limits),patch('win32job.IsProcessInJob',return_value=True):
                with self.assertRaisesRegex(RuntimeError,'메모리 제한'):ledger.register_launch('word',process,MagicMock(),'Word.exe')
            self.assertEqual(ledger.owned,[])

    def test_foreign_pid_is_never_ledgered_or_budgeted_even_after_an_owned_binding(self):
        import subprocess
        from magic_converter.core.jobs.process_runner import start_process,stop_process
        with tempfile.TemporaryDirectory() as directory:
            ledger=ProcessLedger(Path(directory)/'report.processes.json')
            children=[]
            try:
                for _ in range(2):children.append(start_process([sys.executable,'-c','import time;time.sleep(30)'],
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,memory_limit=2*1024**3))
                (owned,job),(foreign,_) = children
                ledger.register_launch('word',owned,job,sys.executable)
                app=MagicMock();app.Windows.Count=1;ledger.owned_apps[id(app)]=app
                with patch('win32process.GetWindowThreadProcessId',return_value=(1,foreign.pid)),patch.object(ledger.budget,'attach') as assign:
                    with self.assertRaisesRegex(RuntimeError,'경계'):ledger.track(app,'word')
                    self.assertFalse(ledger.is_owned(app))
                    with self.assertRaisesRegex(RuntimeError,'소유권|예산'):ledger.require_owned(app,'word')
                assign.assert_not_called()
                self.assertEqual([x['pid'] for x in ledger.owned],[owned.pid])
                self.assertIsNone(foreign.poll())
            finally:
                for process,job in children:stop_process(process,job)


if __name__=='__main__':unittest.main()
