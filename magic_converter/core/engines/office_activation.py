"""Start installed native servers inside a Job before COM activation.

Only query the ROT for that already-running file. BindToObject/GetObject(path)
could activate another unbounded server and are deliberately not used here.
PowerPoint queries its active object only after an empty automation process
is launched under the budget.
"""
from contextlib import contextmanager
from pathlib import Path
import subprocess
import time

from magic_converter.core.engines.office_budget import OFFICE_COMMIT_BYTES
from magic_converter.core.jobs.job_control import check_cancelled,JobTimedOut
from magic_converter.core.jobs.process_runner import _ProcessDirectory,start_process,stop_process

STARTUP_SECONDS = 25


def _bootstrap(folder, kind):
    path = folder / ('bootstrap.docx' if kind == 'word' else 'bootstrap.xlsx')
    if kind == 'word':
        from docx import Document
        Document().save(path)
    else:
        from openpyxl import Workbook
        book = Workbook()
        try:book.save(path)
        finally:book.close()
    return path


def _command(kind, path):
    from magic_converter.core.engines.office_engine import find_office
    if kind == 'powerpoint':
        executable = find_office('PowerPoint')
        if not executable:raise RuntimeError('PowerPoint 실행 파일을 확인하지 못했습니다.')
        return [executable,'/EMBEDDING']
    executable = find_office({'word':'Word','excel':'Excel'}[kind])
    if not executable:
        raise RuntimeError('Office 실행 파일을 확인하지 못해 제한된 자동화를 시작하지 않았습니다.')
    # The only startup input is our macro-free blank, never a user's file.
    # Excel /s can block on a global recovery notice before this blank opens.
    # /automation suppresses automatically opened files and auto-run macros
    # without changing/deleting the user's recovery records. The process is
    # still assigned to its Job before resume; user input waits for ownership.
    # https://learn.microsoft.com/en-us/troubleshoot/microsoft-365-apps/excel/files-open-automatically
    switches = ['/a','/q','/n'] if kind == 'word' else ['/x','/e','/automation']
    return [executable,*switches,str(path)]


def _running_application(kind, previous):
    import pythoncom
    import win32com.client
    try:
        # GetActiveObject is a lookup: it cannot activate an SCM server.
        unknown = pythoncom.GetActiveObject('PowerPoint.Application')
        return win32com.client.Dispatch(unknown.QueryInterface(pythoncom.IID_IDispatch))
    except pythoncom.com_error:return None


def powerpoint_pids():
    """PowerPoint is multi-use; never let a launch redirect into a user server."""
    from magic_converter.core.jobs.windows_processes import process_snapshot
    return {pid for pid,item in process_snapshot().items() if item['image'].lower()=='powerpnt.exe'}


def _refuse_existing_powerpoint():
    from magic_converter.core.engines.office_com import ExistingOfficeServerError
    if powerpoint_pids():
        raise ExistingOfficeServerError('기존 Office 서버(PowerPoint)를 재사용할 수 없습니다. 문서를 보존하고 입력을 거부했습니다.')


def _running_document(path):
    import pythoncom
    import win32com.client
    try:
        rot = pythoncom.GetRunningObjectTable()
        unknown = rot.GetObject(pythoncom.CreateFileMoniker(str(path.resolve())))
    except pythoncom.com_error:
        return None
    return win32com.client.Dispatch(unknown.QueryInterface(pythoncom.IID_IDispatch))


def _validate_application(app, process, job, kind, hwnd=None):
    import win32api
    import win32job
    import win32process
    from magic_converter.core.engines.office_com import UnownedOfficeServerError
    if hwnd is None:
        from magic_converter.core.engines.office_com import application_hwnd
        hwnd = application_hwnd(app,kind,process.pid)
    if not hwnd:raise UnownedOfficeServerError('실행한 Office 서버의 창 식별 정보를 확인하지 못했습니다.')
    _,pid = win32process.GetWindowThreadProcessId(hwnd)
    if pid != process.pid:
        raise UnownedOfficeServerError('전용 Office 문서가 실행한 서버와 달라 입력을 열지 않았습니다.')
    handle = win32api.OpenProcess(0x0400,False,pid)
    try:
        if (str(win32process.GetProcessTimes(handle)['CreationTime']) !=
                str(win32process.GetProcessTimes(process._handle)['CreationTime'])
                or not win32job.IsProcessInJob(handle,job)):
            raise UnownedOfficeServerError('Office 실행 전 메모리 예산의 소유권을 확인하지 못했습니다.')
    finally:handle.Close()
    return app


def _validate_document(document, process, job, path, kind):
    from magic_converter.core.engines.office_com import UnownedOfficeServerError
    if Path(str(document.FullName)).resolve() != path.resolve():
        raise UnownedOfficeServerError('전용 Office 빈 문서의 경로가 일치하지 않습니다.')
    app = document.Application
    value = document.Windows.Item(1).Hwnd if kind == 'word' else app.Hwnd
    return _validate_application(app,process,job,kind,int(value() if callable(value) else value))


def _remove_private_recent(app, path):
    """Delete only this random private path from Office's recent-file list."""
    private = path.resolve()
    recent = app.RecentFiles
    for index in range(min(int(recent.Count),200),0,-1):
        item = recent.Item(index)
        parent,name = str(item.Path),str(item.Name)
        for candidate in (Path(parent),Path(parent)/name):
            if candidate.name.casefold() != path.name.casefold():
                continue
            try:
                matches = candidate.resolve() == private
            except OSError:
                # Unrelated or unavailable recent documents remain untouched.
                continue
            if matches:
                item.Delete()
                break


@contextmanager
def bounded_application(kind, ledger):
    if kind not in {'word','excel','powerpoint'}:raise ValueError('Unsupported bounded Office application')
    with _ProcessDirectory(prefix='magic-office-launch-') as directory:
        document_server = kind in {'word','excel'}
        if kind == 'powerpoint':_refuse_existing_powerpoint()
        previous = set()
        path = _bootstrap(Path(directory),kind) if document_server else None
        command = _command(kind,path)
        check_cancelled()
        process,job = start_process(command,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,
                                    memory_limit=OFFICE_COMMIT_BYTES,hidden=True,cwd=str(Path(command[0]).parent))
        document = app = None
        try:
            ledger.register_launch(kind,process,job,command[0])
            started = time.monotonic()
            while True:
                check_cancelled()
                if process.poll() is not None:
                    raise RuntimeError(f'제한된 Office 프로세스가 연결 전에 종료됐습니다 ({process.returncode}).')
                if document_server:
                    document = _running_document(path)
                    if document is not None:break
                else:
                    app = _running_application(kind,previous)
                    if app is not None:
                        from magic_converter.core.engines.office_com import application_hwnd
                        hwnd = application_hwnd(app,kind,process.pid)
                        if hwnd:
                            _validate_application(app,process,job,kind,hwnd)
                            break
                if time.monotonic()-started >= STARTUP_SECONDS:
                    raise JobTimedOut('제한된 Office 빈 문서 연결 시간이 초과됐습니다.')
                time.sleep(.05)
            app = (_validate_document(document,process,job,path,kind) if document_server
                   else _validate_application(app,process,job,kind))
            ledger.require_owned(app,kind)
            if document_server:
                _remove_private_recent(app,path)
                app.Visible = False
                document.Close(False)
                document = None
            yield app
        finally:
            # An unverified ROT object is never closed or quit. Our explicit
            # Popen/Job remains safe to stop even when binding or COM hangs.
            document = app = None
            stop_process(process,job)
            ledger.launched.pop(kind,None)
