
from __future__ import annotations

import gc
import json
import os
import re
import tempfile
import time
from weakref import WeakValueDictionary
from contextlib import contextmanager, ExitStack, nullcontext
from functools import lru_cache
from itertools import combinations
from pathlib import Path


class ExistingOfficeServerError(RuntimeError):
    """A known existing server cannot safely replace its current document."""


class UnownedOfficeServerError(ExistingOfficeServerError):
    """No positive process identity/budget; registration repair cannot fix this."""


@lru_cache(maxsize=8)
def com_available(progid: str) -> bool:
    if os.name != "nt":
        return False
    import winreg
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, progid + "\\CLSID", 0, winreg.KEY_READ | view):
                return True
        except OSError:
            continue
    return False




@contextmanager
def com_apartment():
    """Balance initialization even when Open/SaveAs/Quit raises an exception."""
    if os.name != "nt":
        yield
        return
    import pythoncom
    pythoncom.CoInitialize()
    try:
        yield
    finally:
        pythoncom.CoUninitialize()


def _save_json(path: Path, data: dict) -> None:
    staging = path.with_suffix(".tmp")
    staging.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    # The supervisor briefly opens this file while polling. Windows readers
    # without FILE_SHARE_DELETE can temporarily prevent atomic replacement.
    for attempt in range(11):
        try:
            os.replace(staging, path)
            break
        except PermissionError:
            if attempt == 10:
                raise
            time.sleep(0.02)


def _powerpoint_hwnd(app, expected_pid=None) -> int:
    try:
        value = app.HWND
        return int(value() if callable(value) else value)
    except Exception:
        # Identity lookup must not change even a temporary property on an
        # unverified (possibly user-owned) application.
        if expected_pid is None:return 0
        from magic_converter.core.engines.office_activation import powerpoint_pids
        if powerpoint_pids() != {expected_pid}:return 0
        import win32gui
        import win32process
        caption = str(app.Caption)
        handles = []
        def visit(hwnd, _):
            if (win32process.GetWindowThreadProcessId(hwnd)[1] == expected_pid
                    and win32gui.GetClassName(hwnd) == 'PPTFrameClass'
                    and caption and caption in win32gui.GetWindowText(hwnd)):
                handles.append(hwnd)
        win32gui.EnumWindows(visit,None)
        return handles[0] if len(handles)==1 else 0


def application_hwnd(app, kind, expected_pid=None):
    if kind == 'powerpoint':return _powerpoint_hwnd(app,expected_pid)
    if kind == 'hwp':
        pass
        for name in ('WindowHandle','Handle','Hwnd'):
            try:
                value = getattr(window,name)
                return int(value() if callable(value) else value)
            except Exception:continue
        return 0
    if kind == 'word':
        if app.Windows.Count == 0:return 0
        value = app.Windows.Item(1).Hwnd
    else:value = app.Hwnd
    return int(value() if callable(value) else value)


class ProcessLedger:
    def __init__(self, path: Path):
        import win32process
        from magic_converter.core.engines.office_budget import OfficeProcessBudget
        self.path, self.owned = path, []
        self.before = set(win32process.EnumProcesses())
        self.budget = OfficeProcessBudget(self.before)
        self.unowned_apps = WeakValueDictionary()
        self.owned_apps = WeakValueDictionary()
        self.app_processes = {}
        self.graceful_exits = set()
        self.launched = {}
        _save_json(path, {"owned": []})

    def register_launch(self, kind, process, job, image):
        import win32job
        import win32process
        limits = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        from magic_converter.core.engines.office_budget import OFFICE_COMMIT_BYTES
        required = (win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE | win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY
                    | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY)
        if (process.pid in self.before or not win32job.IsProcessInJob(process._handle, job)
                or limits['BasicLimitInformation']['LimitFlags'] & required != required
                or limits['ProcessMemoryLimit'] != OFFICE_COMMIT_BYTES
                or limits['JobMemoryLimit'] != OFFICE_COMMIT_BYTES):
            raise UnownedOfficeServerError('Office 실행 전 메모리 제한을 확인하지 못했습니다.')
        identity = {'pid':process.pid, 'created':str(win32process.GetProcessTimes(process._handle)['CreationTime']),
                    'commit_limit_bytes':OFFICE_COMMIT_BYTES, 'budget_phase':'before_process_resume',
                    'activation':'direct_launch_rot', 'image':str(image), 'application':kind}
        self.launched[kind] = (identity, job)
        self.owned.append(identity)
        _save_json(self.path, {'owned':self.owned})

    def is_owned(self, app) -> bool:
        return self.owned_apps.get(id(app)) is app

    def require_owned(self, app, kind: str) -> None:
        self.track(app, kind)
        if not self.is_owned(app):
            self.exclude(app)
            raise UnownedOfficeServerError('Office 서버 소유권·메모리 예산을 확인하지 못해 입력을 열지 않았습니다.')

    def exclude(self, app) -> None:
        # Expire the identity with the proxy; do not retain a user's interface
        # beyond its COM apartment or leave an id that a new proxy can reuse.
        self.unowned_apps[id(app)] = app
        self.owned_apps.pop(id(app), None)
        self.app_processes.pop(id(app), None)

    def mark(self, stage: str, detail: str = "") -> None:
        target = (self.path.with_name(self.path.name.replace(".processes.json", ".state.json"))
                  if self.path.name.endswith(".processes.json") else self.path.with_suffix(".state.json"))
        # Extra diagnostics within a stage must not extend its time budget.
        if getattr(self, "_stage", None) != stage:
            self._stage, self._stamp = stage, time.monotonic_ns()
        history = getattr(self, '_history', [])
        if not history or (history[-1]['stage'], history[-1]['detail']) != (stage, detail):
            history = (history + [{"stage": stage, "detail": detail[:160], "stamp": self._stamp}])[-32:]
            self._history = history
        _save_json(target, {"stage": stage, "detail": detail, "stamp": self._stamp, "history": history})

    def track(self, app, kind: str) -> None:
        if self.unowned_apps.get(id(app)) is app:
            return
        import win32api
        import win32process
        hwnd = None
        try:
            expected_pid = self.launched[kind][0]['pid'] if kind in self.launched else None
            hwnd = application_hwnd(app,kind,expected_pid)
            if not hwnd:
                return
            _, pid = win32process.GetWindowThreadProcessId(hwnd)
            if pid in self.before:
                self.exclude(app)
                return
            handle = win32api.OpenProcess(0x0400 | 0x0010, False, pid)
            try:
                created = str(win32process.GetProcessTimes(handle)["CreationTime"])
            finally:
                handle.Close()
            identity = {"pid": pid, "created": created}
        except Exception:
            # No HWND means no positive process ownership or native Job.
            # Older interfaces/bootstrap remain a separately reported gap.
            return
        if kind in self.launched:
            import win32job
            expected, job = self.launched[kind]
            handle = win32api.OpenProcess(0x0400, False, pid)
            try:
                if ((pid, created) != (expected['pid'], expected['created'])
                        or not win32job.IsProcessInJob(handle, job)):
                    self.exclude(app)
                    raise UnownedOfficeServerError('실행한 Office 서버의 메모리 경계를 벗어났습니다.')
            finally:handle.Close()
        existing = next((item for item in self.owned if item['pid'] == pid and item['created'] == created), None)
        if existing is not None:
            identity = existing
        else:
            self.owned.append(identity)
            _save_json(self.path, {"owned": self.owned})
        # A launched session must pass its exact identity check before any
        # ledger/cleanup eligibility. Legacy SCM identities still fail closed
        # when positive ownership cannot receive its requested native budget.
        if kind not in self.launched and not self.budget.attach(pid, created):
            raise RuntimeError("Office 서버의 프로세스 식별 정보가 변경되었습니다.")
        if kind not in self.launched:
            identity.update(application=kind, activation='scm', budget_phase='after_native_identity',
                            preactivation_bounded=False)
        self.owned_apps[id(app)] = app
        self.app_processes[id(app)] = (pid, created)
        identity['commit_limit_bytes'] = self.budget.memory_limit
        _save_json(self.path, {"owned": self.owned})


def cleanup_owned_processes(path: Path) -> None:
    """Never terminate by application name or touch a pre-existing server."""
    if not path.is_file():
        return
    import win32api
    import win32process
    for identity in json.loads(path.read_text(encoding="utf-8")).get("owned", []):
        try:
            handle = win32api.OpenProcess(0x0400 | 0x0001, False, identity["pid"])
            try:
                if str(win32process.GetProcessTimes(handle)["CreationTime"]) == identity["created"]:
                    win32api.TerminateProcess(handle, 1)
            finally:
                handle.Close()
        except Exception:
            continue


@contextmanager
def application(kind: str, ledger: ProcessLedger):
    import win32com.client
    app = None
    previous_count = 0
    old_security = None
    old_alerts = None
    old_visible = None
    preserve_server = False
    activation = ExitStack()
    try:
        stage = "starting" if kind == "hwp" else "office"
        ledger.mark(stage, "bounded process / ROT")
        from magic_converter.core.engines.office_activation import bounded_application
        if kind == 'hwp':
            pass
            pass
            from magic_converter.core.jobs.job_control import check_cancelled
            check_cancelled()
            pass
            pass
            ledger.mark(stage,'DispatchEx')
            # Discovery/status I/O may consume the remaining deadline. Do not
            # start a new SCM server for a request that has already ended.
            # This guard does not impose a preactivation memory limit on SCM.
            check_cancelled()
            app = win32com.client.DispatchEx(progid)
        else:app = activation.enter_context(bounded_application(kind,ledger))
        ledger.mark(stage, "server identity")
        if kind == "hwp":
            ledger.track(app, kind)
            preserve_server = ledger.unowned_apps.get(id(app)) is app
            if preserve_server:
                # Opening another HWP may replace a document in this server.
                # Refuse before registration, mode changes, Open or Clear.
                pass
            ledger.require_owned(app, kind)
            ledger.mark(stage, "generated interface")
            try:
                app = win32com.client.gencache.EnsureDispatch(app)
            except Exception:
                # Frozen builds can have a read-only gen_py cache. Dynamic
                # IDispatch remains fully functional for these API calls.
                pass
            ledger.require_owned(app, kind)
            pass
            pass
            pass
            try:
                # OOXML export uses Continue/Cancel (OKCANCEL), independently
                pass
            except Exception:
                pass
            ledger.mark(stage, "automation window")
            pass
        else:
            collection = {"word": "Documents", "excel": "Workbooks", "powerpoint": "Presentations"}[kind]
            previous_count = getattr(app, collection).Count
            if previous_count:
                # Some Office factories reuse an instance, even one started
                # after our PID snapshot. Existing documents mean this is not
                # our server: neither the Job nor later track calls may own it.
                ledger.exclude(app)
                raise ExistingOfficeServerError('기존 Office 서버를 재사용할 수 없습니다. 새 자동화 서버가 필요합니다.')
            else:
                ledger.track(app, kind)
            preserve_server = ledger.unowned_apps.get(id(app)) is app
            if preserve_server:
                raise ExistingOfficeServerError('기존 Office 서버를 재사용할 수 없습니다. 새 자동화 서버가 필요합니다.')
            if kind != 'word':
                ledger.require_owned(app, kind)
            if kind != "powerpoint":
                old_visible = app.Visible
                if not previous_count and not preserve_server:
                    app.Visible = False
            old_alerts = app.DisplayAlerts
            app.DisplayAlerts = 1 if kind == "powerpoint" else 0
            try:
                old_security = app.AutomationSecurity
                app.AutomationSecurity = 3  # Disable document macros.
            except Exception:
                pass
            if kind == 'word':
                ledger.require_owned(app, kind)
        yield app
    finally:
        if app is not None:
            try:
                ledger.mark("cleanup")
            except BaseException:
                # A failed status write must still close the explicit launch
                # Job even when COM cleanup cannot be entered.
                activation.close()
                raise
            reused = bool(previous_count or preserve_server or ledger.unowned_apps.get(id(app)) is app
                          or not ledger.is_owned(app))
            if kind == "hwp" and not reused:
                try:
                    app.Clear(1)
                except Exception:
                    pass
            if old_security is not None:
                try:
                    app.AutomationSecurity = old_security
                except Exception:
                    pass
            if old_alerts is not None:
                try:
                    app.DisplayAlerts = old_alerts
                except Exception:
                    pass
            if reused and old_visible is not None:
                try:
                    app.Visible = old_visible
                except Exception:
                    pass
            # A reused PowerPoint server with a user's presentation must stay.
            if not reused:
                try:
                    app.Quit()
                except Exception:
                    pass
                else:
                    if kind == 'hwp':
                        identity = ledger.app_processes.get(id(app))
                        if identity is not None:
                            ledger.graceful_exits.add(identity)
            app = None
        activation.close()
        gc.collect()


@contextmanager
def word_document(app, source: Path):
    document = None
    try:
        document = app.Documents.Open(str(source.resolve()), ConfirmConversions=False, ReadOnly=True,
                                      AddToRecentFiles=False, Visible=False, OpenAndRepair=False,
                                      PasswordDocument="", WritePasswordDocument="")
        yield document
    finally:
        if document is not None:
            try:
                document.Close(False)
            finally:
                document = None


def word_save(source: Path, destination: Path, format_id: int, ledger: ProcessLedger) -> None:
    with application("word", ledger) as word:
        with word_document(word, source) as document:
            ledger.track(word, "word")
            if format_id == 17:
                document.ExportAsFixedFormat(str(destination.resolve()), 17, OpenAfterExport=False)
            else:
                document.SaveAs2(str(destination.resolve()), FileFormat=format_id, AddToRecentFiles=False)


def _range_bounds(region):
    row, column = int(region.Row), int(region.Column)
    return row, column, row + int(region.Rows.Count) - 1, column + int(region.Columns.Count) - 1


EXCEL_PRINT_CELLS = 30_000
EXCEL_VALIDATION_PRINTER = 'MAGIC_CONVERTER_EXCEL_VALIDATION_PRINTER'


def _automatic_print_bounds(sheet, setup):
    used = sheet.UsedRange
    bounds = _range_bounds(used)
    # Default Excel pagination can start before the first populated cell.
    if bounds[2] * bounds[3] > EXCEL_PRINT_CELLS:
        raise ValueError(f"자동 인쇄 영역의 합계는 {EXCEL_PRINT_CELLS:,}셀까지 지원합니다.")
    first = used.Cells.Item(1, 1)
    last = used.Cells.Item(used.Rows.Count, used.Columns.Count)
    row_start = used.Find('*', last, -4123, 2, 1, 1, False, False, False)
    if row_start is None:
        raise ValueError("자동 인쇄 영역을 정할 값이나 수식이 없습니다.")
    row_end = used.Find('*', first, -4123, 2, 1, 2, False, False, False)
    col_start = used.Find('*', last, -4123, 2, 2, 1, False, False, False)
    col_end = used.Find('*', first, -4123, 2, 2, 2, False, False, False)
    candidate = (int(row_start.Row), int(col_start.Column), int(row_end.Row), int(col_end.Column))
    shapes = sheet.Shapes
    if (candidate != bounds or used.MergeCells is not False or shapes.Count > 1
            or setup.PrintComments != -4142):
        raise ValueError("자동 인쇄 영역을 정할 수 없습니다. 직사각형 인쇄 영역을 저장하세요.")
    if shapes.Count:
        shape = shapes.Item(1)
        if shape.Type != 1 or shape.AutoShapeType != 1 or shape.Visible != -1 or shape.Rotation != 0:
            raise ValueError("자동 인쇄 영역은 회전하지 않은 표시 직사각형 도형 한 개만 지원합니다.")
        first_cell, last_cell = shape.TopLeftCell, shape.BottomRightCell
        top, left, bottom, right = candidate
        candidate = (min(top, first_cell.Row), min(left, first_cell.Column),
                     max(bottom, last_cell.Row), max(right, last_cell.Column))
    return candidate


def _print_area_cells(bounds, titles):
    top, left, bottom, right = bounds
    effective = []
    for start, end, title in zip((top, left), (bottom, right), titles):
        extent = end - start + 1
        if title is not None:
            title_start, title_end = title
            if title_end > end:
                raise ValueError("반복 인쇄 제목은 인쇄 영역 안이나 위쪽·왼쪽에 있어야 합니다.")
            extent += max(0, min(start - 1, title_end) - title_start + 1)
        effective.append(extent)
    return effective[0] * effective[1]


def _verify_print_areas(workbook):
    """Bound native pagination before letting Excel export the workbook."""
    sheets = workbook.Worksheets
    charts = workbook.Charts
    if workbook.Sheets.Count != sheets.Count + charts.Count:
        raise ValueError("Excel 인쇄 영역 모드는 워크시트와 차트 시트만 지원합니다.")
    if workbook.Sheets.Count > 20:
        raise ValueError("Excel 인쇄 영역 모드는 시트 최대 20개를 지원합니다.")
    visible_charts = sum(charts.Item(index).Visible == -1 for index in range(1, charts.Count + 1))
    if visible_charts > 2:
        raise ValueError("표시된 차트 시트는 두 개까지 지원합니다.")
    total = 0
    cell = r'\$?[A-Z]{1,3}\$?[1-9][0-9]{0,6}'
    title_patterns = (r'\$?[1-9][0-9]{0,6}:\$?[1-9][0-9]{0,6}', r'\$?[A-Z]{1,3}:\$?[A-Z]{1,3}')
    for index in range(1, sheets.Count + 1):
        sheet = sheets.Item(index)
        if sheet.Visible != -1:
            continue
        setup = sheet.PageSetup
        area = setup.PrintArea
        raw_titles = (setup.PrintTitleRows, setup.PrintTitleColumns)
        if not area:
            if any(raw_titles):
                raise ValueError("반복 인쇄 제목을 사용하려면 인쇄 영역을 저장하세요.")
            _, _, bottom, right = _automatic_print_bounds(sheet, setup)
            total += bottom * right
        else:
            addresses = area.split(',')
            if len(addresses) > 3 or not all(re.fullmatch(fr'{cell}(?::{cell})?', address) for address in addresses):
                raise ValueError("각 표시 워크시트에 직사각형 인쇄 영역을 세 개까지 저장하세요.")
            regions = [_range_bounds(sheet.Range(address)) for address in addresses]
            for (top, left, bottom, right), (other_top, other_left, other_bottom, other_right) in combinations(regions, 2):
                if top <= other_bottom and other_top <= bottom and left <= other_right and other_left <= right:
                    raise ValueError("인쇄 영역은 서로 겹칠 수 없습니다.")
            titles = []
            for axis, (title, pattern) in enumerate(zip(raw_titles, title_patterns)):
                if not title:
                    titles.append(None)
                    continue
                if not re.fullmatch(pattern, title):
                    raise ValueError("반복 인쇄 제목은 연속된 행 또는 열로 지정하세요.")
                bounds = _range_bounds(sheet.Range(title))
                titles.append((bounds[axis], bounds[axis + 2]))
            total += sum(_print_area_cells(region, titles) for region in regions)
        if total > EXCEL_PRINT_CELLS:
            raise ValueError(f"Excel 인쇄 영역과 반복 제목의 합계는 {EXCEL_PRINT_CELLS:,}셀까지 지원합니다.")
    if not total and not visible_charts:
        raise ValueError("표시 워크시트의 인쇄 영역이나 표시 차트가 필요합니다.")


def office_pdf(source: Path, destination: Path, ledger: ProcessLedger, *, print_area_only=False) -> None:
    if print_area_only and source.suffix.lower() != ".xlsx":
        raise ValueError("Excel 인쇄 PDF는 XLSX만 지원합니다.")
    if source.suffix.lower() == ".docx":
        word_save(source, destination, 17, ledger)
    elif source.suffix.lower() == ".xlsx":
        with application("excel", ledger) as excel:
            workbook = None
            try:
                ledger.mark('office','open workbook')
                workbook = excel.Workbooks.Open(str(source.resolve()), UpdateLinks=0, ReadOnly=True,
                                                IgnoreReadOnlyRecommended=True, Password="", WriteResPassword="")
                ledger.track(excel, "excel")
                ledger.mark('office_export','export workbook PDF')
                if print_area_only:
                    printer = os.environ.get(EXCEL_VALIDATION_PRINTER)
                    if printer:
                        from hashlib import sha256
                        excel.ActivePrinter = printer
                        identity, _ = ledger.launched['excel']
                        identity['print_queue_sha256'] = sha256(str(excel.ActivePrinter).encode('utf-8')).hexdigest()
                        _save_json(ledger.path, {'owned': ledger.owned})
                    _verify_print_areas(workbook)
                    workbook.ExportAsFixedFormat(0, str(destination.resolve()),
                                                 IgnorePrintAreas=False, OpenAfterPublish=False)
                else:
                    workbook.ExportAsFixedFormat(0, str(destination.resolve()), OpenAfterPublish=False)
                ledger.mark('office','workbook PDF complete')
            finally:
                if workbook is not None:
                    workbook.Close(False)
                    workbook = None
    else:
        with application("powerpoint", ledger) as powerpoint:
            presentation = None
            try:
                presentation = powerpoint.Presentations.Open(str(source.resolve()), ReadOnly=True,
                                                              Untitled=False, WithWindow=False)
                ledger.track(powerpoint, "powerpoint")
                presentation.SaveAs(str(destination.resolve()), 32)
            finally:
                if presentation is not None:
                    presentation.Close()
                    presentation = None


def _hwp_open(hwp, source: Path) -> None:
    if hwp.Open(str(source.resolve()), "", "forceopen:true;versionwarning:false") is False:
        pass


def _hwp_save(hwp, destination: Path, format_name: str) -> None:
    if hwp.SaveAs(str(destination.resolve()), format_name, "") is False:
        pass
    if not destination.is_file() or not destination.stat().st_size:
        pass




def helper_main(request_path: Path, report_path: Path) -> int:
    ledger_path = report_path.with_suffix(".processes.json")
    ledger = None
    try:
        request = json.loads(request_path.read_text(encoding="utf-8"))
        pass
        with com_apartment():
            ledger = ProcessLedger(ledger_path)
            pass
            pass
            source, destination = Path(request["source"]), Path(request["destination"])
            if request["operation"] == "excel_print":
                office_pdf(source, destination, ledger, print_area_only=True)
            else:
                office_pdf(source, destination, ledger)
        _save_json(report_path, {"ok": True})
        return 0
    except Exception as exc:
        _save_json(report_path, {"ok": False, "error": str(exc)})
        return 1
    finally:
        gc.collect()
        if ledger is not None:
            try:
                if ledger.graceful_exits:
                    cleanup = ledger.budget.wait_for_exit(ledger.graceful_exits)
                    _save_json(ledger.path, {'owned': ledger.owned, 'cleanup': cleanup})
            finally:
                ledger.budget.close()
                cleanup_owned_processes(ledger_path)
        else:
            cleanup_owned_processes(ledger_path)


if __name__ == "__main__":
    import sys
    raise SystemExit(helper_main(Path(sys.argv[1]), Path(sys.argv[2])))
