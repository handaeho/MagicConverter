"""Run with python -m magic_converter.main or python magic_converter/main.py."""

from __future__ import annotations

import ctypes
import argparse
import logging
import os
import signal
import sys
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path


def configure_dpi() -> None:
    """Set Per-Monitor V2 awareness before QApplication creates any HWND."""
    if sys.platform != "win32":
        return
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        setter = user32.SetProcessDpiAwarenessContext
        setter.argtypes = [ctypes.c_void_p]
        setter.restype = ctypes.c_bool
        if setter(ctypes.c_void_p(-4)):
            return
        # Access denied means a host/manifest already set process awareness.
        if ctypes.get_last_error() == 5:
            return
    except (AttributeError, OSError):
        logging.getLogger(__name__).debug("Per-Monitor V2 unavailable", exc_info=True)
    try:
        shcore = ctypes.WinDLL("shcore")
        shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
        shcore.SetProcessDpiAwareness.restype = ctypes.c_long
        shcore.SetProcessDpiAwareness(2)
    except (AttributeError, OSError):
        logging.getLogger(__name__).debug("Per-Monitor awareness unavailable", exc_info=True)


if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from PySide6.QtCore import QLockFile, QObject, QSettings, QStandardPaths, Qt, QTimer, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication, QMessageBox, QSystemTrayIcon

INI_SETTINGS_FORMAT = QSettings.Format.IniFormat

from magic_converter.core.conversion.converter import ConversionService
from magic_converter import __version__
from magic_converter.core.platform.hook_manager import HookManager
from magic_converter.core.settings.preferences import default_preferences
from magic_converter.core.settings.settings_store import SettingsStore
from magic_converter.core.settings.history import HistoryStore
from magic_converter.ui.shell.radial_wheel import RadialWheelWindow
from magic_converter.ui.jobs.progress_overlay import ProgressOverlay
from magic_converter.ui.shell.tray_icon import TrayIcon
from magic_converter.ui.common.icons import make_icon


class ApplicationManager(QObject):
    def __init__(self, app: QApplication, *, data_dir: Path | None = None, isolated: bool = False) -> None:
        super().__init__(app)
        self.app = app
        self.settings = (QSettings(str(data_dir / "legacy.ini"), INI_SETTINGS_FORMAT) if isolated and data_dir else
                         QSettings("magic_converter", "magic_converter"))
        legacy = default_preferences().base_values()
        for key, fallback in legacy.copy().items():
            try:
                legacy[key] = self.settings.value(key, fallback, type=type(fallback))
            except (TypeError, ValueError):
                pass
        data_dir = data_dir or (Path(self.settings.fileName()).parent if self.settings.format() == INI_SETTINGS_FORMAT else
                               Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)))
        self.data_dir = data_dir
        self.settings_store = SettingsStore(data_dir / "settings.json")
        try:
            self.preferences = self.settings_store.load(legacy)
        except OSError as exc:
            self.preferences = default_preferences()
            self.settings_store.read_only = True
            self.settings_store.notice = f"설정 파일에 접근할 수 없습니다: {exc}"
        self.values = self.preferences.base_values()
        # Native automation helpers still read the legacy consent flag. Keep
        # that mirror in sync after migration/import/recovery as well as Save.
        if not self.settings_store.read_only:
            for key, value in self.values.items():
                self.settings.setValue(key, value)
            self.settings.sync()
        self.history = HistoryStore(data_dir / "history.json", self.preferences.history_limit)
        self.exiting = False
        self._cleaned_up = False
        self.service = ConversionService(self, self.values["ffmpeg_path"])
        self.service.preferences = self.preferences
        self.watch, self.automation_store, self.ipc = None, None, None
        automation_notice = ""
        try:
            from magic_converter.core.automation.automation_store import AutomationStore
            from magic_converter.core.automation.watch_controller import WatchController
            self.automation_store = AutomationStore(data_dir)
            self.watch = WatchController(self.service, self.automation_store, self)
        except Exception as exc:
            automation_notice = f"자동화를 시작하지 못했습니다. 기존 설정·기록은 보존했습니다: {exc}"
        self.wheel = RadialWheelWindow(lambda: self.service.ffmpeg is not None, preferences=lambda: self.preferences)
        self.wheel.web_output = lambda: self.automation_store.web_output if self.automation_store else ""
        self.progress_overlay = ProgressOverlay()
        self.hook = HookManager(self, interval_ms=self.values["interval_ms"], threshold=self.values["threshold"])
        self.tray = TrayIcon(self.values, self)
        self.tray.set_conversion_service(self.service)
        self.tray.set_preferences(self.preferences, self.settings_store)
        self.tray.set_history(self.history)
        self.hook.wheel_requested.connect(self.wheel.show_for_drag)
        self.hook.dismiss_requested.connect(self.wheel.request_dismiss)
        self.hook.error.connect(self._hook_error)
        self.wheel.drag_finished.connect(self.hook.consume_drag)
        self.wheel.request_dropped.connect(self.service.submit_selection)
        self.wheel.input_rejected.connect(self.tray.notify_error)
        self.service.completed.connect(self._completed)
        if self.watch:
            self.watch.completed.connect(self._publish_result)
            self.watch.error.connect(self.tray.notify_error)
            self.watch.changed.connect(lambda: self._busy_changed(self.service.active_count))
        self.service.progress_changed.connect(self.progress_overlay.update_progress)
        self.service.progress_changed.connect(self.tray.set_progress)
        self.service.failed.connect(self.tray.notify_error)
        self.service.busy_changed.connect(self._busy_changed)
        self.tray.enabled_changed.connect(self._set_enabled)
        self.tray.settings_changed.connect(self._apply_settings)
        self.tray.quit_requested.connect(self.request_quit)
        self.tray.cancel_requested.connect(self.service.cancel_all)
        if self.watch:
            self.tray.cancel_requested.connect(self.watch.stop)
        self.app.aboutToQuit.connect(self._cleanup)
        self.hook.set_enabled(self.values["enabled"])
        self.tray.show()
        self._input_dialog = self._automation_dialog = None
        self._open_paths = ()
        self._open_timer = QTimer(self)
        self._open_timer.setSingleShot(True)
        self._open_timer.timeout.connect(self._show_inputs)
        from PySide6.QtGui import QAction
        for label, callback in (("파일 선택해 변환…", self.choose_inputs), ("감시 폴더 / 웹 드래그 / 탐색기…", self.show_automation)):
            action = QAction(label, self.tray.menu)
            action.triggered.connect(callback)
            self.tray.menu.insertAction(self.tray.quit_action, action)
        if self.watch and any(rule.enabled for rule in self.automation_store.rules):
            QTimer.singleShot(0, self._start_watch)
        pass
        notice = "\n".join(filter(None, (self.settings_store.notice, self.history.notice, automation_notice)))
        if notice:
            logging.getLogger(__name__).warning(notice)
            QTimer.singleShot(0, lambda: self.tray.notify_error(notice) if not self.exiting else None)

    def show_startup(self, tray_only: bool = False) -> None:
        if not self.exiting and not tray_only:
            self.tray.show_settings()

    def start_ipc(self):
        from magic_converter.core.automation.local_ipc import IpcServer
        from magic_converter.core.automation.application_api import ApplicationApi
        self.api = ApplicationApi(self.service, lambda: self.preferences, self.history, watch=self.watch,
                                  open_files=self.open_inputs, settings=self.tray.show_settings,
                                  stop=lambda: QTimer.singleShot(0, self.request_quit))
        self.api.closing = lambda: self.exiting
        try:
            self.ipc = IpcServer(self.data_dir, self._ipc_request, self)
            self.api.instance = self.ipc.instance
            self.ipc.start()
        except Exception as exc:
            logging.getLogger(__name__).exception("IPC could not start; tray conversion remains available")
            self.ipc = None
            message = f"다른 실행에서 요청을 받을 수 없습니다. 기존 IPC 기록은 보존했습니다: {exc}"
            QTimer.singleShot(0, lambda: self.tray.notify_error(message))

    def _ipc_request(self, request):
        return self.api.handle(request)

    def _start_watch(self):
        try:
            if self.watch and not self.exiting:
                self.watch.start()
        except (OSError, ValueError) as exc:
            self.tray.notify_error(str(exc))

    def choose_inputs(self):
        from PySide6.QtWidgets import QFileDialog
        paths, _ = QFileDialog.getOpenFileNames(None, "변환할 파일 선택")
        if paths:
            self.open_inputs(paths)

    def open_inputs(self, paths):
        from magic_converter.core.conversion.file_router import normalize_paths
        self._open_paths = normalize_paths((*self._open_paths, *paths))
        self._open_timer.start(300)

    def _show_inputs(self):
        from magic_converter.ui.automation.input_dialog import InputDialog
        paths, self._open_paths = self._open_paths, ()
        if self._input_dialog:
            self._input_dialog.add_paths(paths)
        else:
            dialog = InputDialog(paths, self.preferences, self.service.ffmpeg)
            self._input_dialog = dialog
            dialog.submitted.connect(self.service.submit_selection)
            def finished():
                self._input_dialog = None
                dialog.deleteLater()
            dialog.finished.connect(finished)
        self._input_dialog.show()
        self._input_dialog.raise_()
        self._input_dialog.activateWindow()

    def show_automation(self):
        if not self.watch:
            self.tray.notify_error("자동화 설정·기록을 읽을 수 없습니다. 로그를 확인하세요.")
            return
        if self._automation_dialog is None:
            from magic_converter.ui.automation.automation_dialog import AutomationDialog
            dialog = AutomationDialog(self.watch, self.preferences)
            self._automation_dialog = dialog
            def finished():
                self._automation_dialog = None
                dialog.deleteLater()
            dialog.finished.connect(finished)
        self._automation_dialog.show()
        self._automation_dialog.raise_()

    def _completed(self, result):
        if self.watch and self.watch.handle_result(result):
            return
        self._publish_result(result)

    def _publish_result(self, result):
        self._record_result(result)
        self.tray.notify_result(result)
        self.progress_overlay.finish_job(result)

    @Slot(str)
    def _hook_error(self, message: str) -> None:
        self.tray.enabled_action.setChecked(False)
        self.tray.notify_error(message)

    @Slot(bool)
    def _set_enabled(self, enabled: bool) -> None:
        self._apply_settings({"enabled": enabled})

    @Slot(object)
    def _apply_settings(self, values: dict) -> None:
        try:
            base = {key: values.get(key, current) for key, current in self.values.items()}
            preferences = replace(values.get("preferences", self.preferences), **base)
            self.settings_store.save(preferences)
        except (OSError, ValueError) as exc:
            self.tray.set_preferences(self.preferences, self.settings_store)
            self.tray.notify_error(f"설정을 저장하지 못했습니다. 이전 설정을 유지합니다.\n{exc}")
            return
        self.preferences = preferences
        self.values = preferences.base_values()
        self.service.preferences = preferences
        self.history.limit = preferences.history_limit
        self.tray.set_preferences(preferences, self.settings_store)
        for key, value in self.values.items():
            self.settings.setValue(key, value)
        self.settings.sync()
        pass
        pass
        self.service.ffmpeg_path = preferences.ffmpeg_path
        self.hook.threshold = preferences.threshold
        self.hook.timer.setInterval(preferences.interval_ms)
        self.hook.set_enabled(preferences.enabled and not self.exiting)

    @Slot(object)
    def _record_result(self, result) -> None:
        try:
            self.history.append(result)
        except (OSError, ValueError) as exc:
            logging.getLogger(__name__).exception("Could not persist conversion history")
            self.tray.notify_error(f"변환은 완료됐지만 결과 기록을 저장하지 못했습니다: {exc}")

    @Slot(int)
    def _busy_changed(self, count: int) -> None:
        if self._cleaned_up:
            return
        if self.exiting and count == 0 and not (self.watch and self.watch.active_count):
            self.app.quit()
        elif not self.exiting:
            self.tray.set_busy(count)

    @Slot()
    def request_quit(self) -> None:
        if self.exiting:
            return
        self.exiting = True
        self.hook.set_enabled(False)
        self.wheel.dismiss()
        if self.watch:
            self.watch.stop()
        if self.service.active_count or self.watch and self.watch.active_count:
            self.tray.begin_shutdown()
        else:
            self.app.quit()

    @Slot()
    def _cleanup(self) -> None:
        if self._cleaned_up:
            return
        # watch.stop() emits changed synchronously. Mark teardown before it
        # can request another quit from inside QApplication.aboutToQuit.
        self._cleaned_up = True
        self.exiting = True
        self.hook.set_enabled(False)
        self.wheel.dismiss()
        self.progress_overlay.shutdown()
        if self.ipc:
            self.ipc.close()
        if self.watch:
            self.watch.stop()
        for dialog in (self._input_dialog, self._automation_dialog):
            if dialog:
                dialog.reject()
        if self.tray._settings_dialog is not None:
            self.tray._settings_dialog.reject()
        if self.tray._jobs_dialog is not None:
            self.tray._jobs_dialog.reject()
        if self.tray._history_dialog is not None:
            self.tray._history_dialog.reject()
        self.tray.hide()


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    from magic_converter.cli import COMMANDS
    if argv and argv[0] in COMMANDS or not argv and Path(sys.executable).stem.endswith("-cli"):
        from magic_converter.cli import run
        return run(argv or ["--help"])
    parser = argparse.ArgumentParser(prog="magic_converter", description="오프라인 방사형 파일 변환 도구")
    parser.add_argument("--tray", action="store_true", help="시작 상태 창을 열지 않고 시스템 트레이에서 실행")
    parser.add_argument("--self-test", metavar="REPORT_JSON", help="창을 띄우지 않고 내장 엔진·Qt를 검증해 JSON 보고서 저장")
    parser.add_argument("--self-test-office", action="store_true", help="자체 검사에 설치된 PowerPoint/LibreOffice의 PPTX 출력을 포함")
    parser.add_argument("--office-com", nargs=2, metavar=("REQUEST", "REPORT"), help=argparse.SUPPRESS)
    parser.add_argument("--web-fetch", nargs=2, metavar=("REQUEST", "REPORT"), help=argparse.SUPPRESS)
    parser.add_argument("--specialized-convert", nargs=2, metavar=("REQUEST", "REPORT"), help=argparse.SUPPRESS)
    parser.add_argument("--open", nargs="+", metavar="FILE", help="탐색기 파일 선택을 변환 창에서 열기")
    parser.add_argument("--data-dir", help="설정·IPC·감시 이력을 분리할 폴더")
    args = parser.parse_args(argv)
    if args.specialized_convert:
        from magic_converter.core.jobs.specialized_worker import helper_main
        return helper_main(*args.specialized_convert)
    if args.web_fetch:
        from magic_converter.core.automation.web_transfer import helper_main
        return helper_main(*args.web_fetch)
    if args.office_com:
        from magic_converter.core.engines.office_com import helper_main
        return helper_main(Path(args.office_com[0]), Path(args.office_com[1]))
    if args.self_test:
        from magic_converter.core.diagnostics.diagnostics import run_self_test
        return run_self_test(Path(args.self_test), test_office=args.self_test_office)
    if sys.platform != "win32":
        print("magic_converter는 Windows 10/11에서 실행하세요.", file=sys.stderr)
        return 1
    configure_dpi()
    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(Qt.HighDpiScaleFactorRoundingPolicy.PassThrough)
    app = QApplication([sys.argv[0]])
    app.setApplicationName("magic_converter")
    app.setOrganizationName("magic_converter")
    app.setApplicationVersion(__version__)
    app.setQuitOnLastWindowClosed(False)
    app.setWindowIcon(make_icon())
    if not QSystemTrayIcon.isSystemTrayAvailable():
        QMessageBox.critical(None, "magic_converter", "사용 가능한 Windows 시스템 트레이가 없습니다.")
        return 1
    from magic_converter.cli import data_directory
    data_dir = data_directory(args.data_dir)
    isolated = bool(args.data_dir or os.environ.get("MAGIC_CONVERTER_PROFILE_DIR"))
    if isolated:
        os.environ["MAGIC_CONVERTER_PROFILE_DIR"] = str(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    lock = QLockFile(str(data_dir / "magic_converter.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        try:
            from magic_converter.core.automation.local_ipc import call
            response = call(data_dir, {"command": "open", "paths": [str(Path(path).resolve()) for path in args.open]}
                            if args.open else {"command": "settings"})
            if response.get("ok"):
                return 0
        except (OSError, ValueError, TimeoutError):
            pass
        message = ("이미 실행 중이거나 실행 잠금 파일에 접근할 수 없습니다.\n\n"
                   "작업 표시줄 오른쪽 아래의 숨겨진 아이콘(^)에서\n"
                   "주황색 magic_converter 아이콘을 우클릭해 설정을 여세요.\n"
                   "업데이트한 코드를 적용하려면 기존 앱을 종료한 뒤 다시 실행하세요.")
        if sys.stdout is not None:
            print(message, flush=True)
        QMessageBox.information(None, "magic_converter", message)
        return 0
    handler = RotatingFileHandler(data_dir / "magic_converter.log", maxBytes=1_000_000, backupCount=2, encoding="utf-8")
    logging.basicConfig(level=logging.INFO, handlers=[handler],
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)

    def exception_hook(exc_type: type, exc: BaseException, traceback: object) -> None:
        logging.getLogger(__name__).critical("Unhandled UI exception", exc_info=(exc_type, exc, traceback))
        QMessageBox.critical(None, "magic_converter", f"오류: {exc}\n로그: {data_dir}")

    sys.excepthook = exception_hook
    manager = ApplicationManager(app, data_dir=data_dir, isolated=isolated)
    manager.start_ipc()
    logging.info("magic_converter started; FFmpeg: %s", manager.service.ffmpeg or "not found")
    if sys.stdout is not None:
        print("magic_converter 실행 중 · Shift+파일 드래그: 변환 / Ctrl+파일 드래그: 액션", flush=True)
        print(f"로그: {data_dir / 'magic_converter.log'}", flush=True)
        print("종료: 상태 창의 '프로그램 종료', 트레이 메뉴 또는 이 터미널에서 Ctrl+C", flush=True)
    QTimer.singleShot(0, lambda: manager.open_inputs(args.open) if args.open else manager.show_startup(tray_only=args.tray))
    previous_sigint = signal.signal(signal.SIGINT, lambda *_: manager.request_quit())
    # Dispatch Python's Ctrl+C handler even when global input polling is off.
    interrupt_timer = QTimer(app)
    interrupt_timer.setInterval(250)
    interrupt_timer.timeout.connect(lambda: None)
    interrupt_timer.start()
    try:
        return app.exec()
    finally:
        signal.signal(signal.SIGINT, previous_sigint)
        interrupt_timer.stop()
        manager._cleanup()
        lock.unlock()
        handler.close()


if __name__ == "__main__":
    raise SystemExit(main())

