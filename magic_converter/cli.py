"""Headless commands using the GUI's immutable recipes and conversion service."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import uuid
import math
import io
from contextlib import redirect_stdout
from dataclasses import asdict, replace
from pathlib import Path

COMMANDS = {"convert", "watch", "serve", "send", "status", "cancel", "stop", "actions", "formats", "presets", "shell"}


def data_directory(override=None):
    override = override or os.environ.get("MAGIC_CONVERTER_PROFILE_DIR")
    if override:
        return Path(override).expanduser().resolve()
    from PySide6.QtCore import QStandardPaths
    return Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation))


def result_dict(result):
    return {"job_id": result.job_id, "action": result.action.key, "completed": result.completed,
            "total": result.total, "cancelled": result.cancelled, "timed_out": result.timed_out,
            "outputs": [str(path) for path in result.outputs], "errors": result.errors, "warnings": result.warnings,
            "files": [{**asdict(file), "source": str(file.source), "outputs": [str(path) for path in file.outputs]}
                      for file in result.files]}


def load_preferences(directory):
    from PySide6.QtCore import QSettings
    from magic_converter.core.settings.preferences import default_preferences
    from magic_converter.core.settings.settings_store import SettingsStore
    legacy = default_preferences().base_values()
    settings = (QSettings(str(directory / "legacy.ini"), QSettings.Format.IniFormat)
                if os.environ.get("MAGIC_CONVERTER_PROFILE_DIR") else QSettings("magic_converter", "magic_converter"))
    for key, value in tuple(legacy.items()):
        legacy[key] = settings.value(key, value, type=type(value))
    return SettingsStore(directory / "settings.json").load(legacy)


def select_recipe(paths, preferences, *, action=None, preset=None, to=None, output=None, catalog=None):
    from magic_converter.core.conversion.file_router import ALL_ACTIONS
    from magic_converter.core.settings.preferences import Preset, OutputRule
    if preset:
        matches = [p for p in preferences.presets if p.id == preset or p.name == preset]
        if len(matches) != 1:
            raise ValueError("이름 또는 ID가 정확히 일치하는 프리셋 하나를 지정하세요.")
        recipe = matches[0]
    else:
        if to:
            from magic_converter.core.specialized.special_formats import target_action
            action = target_action(paths, to, catalog)
        if action not in ALL_ACTIONS or action in {"copy_path", "terminal"}:
            raise ValueError("지원하는 변환 작업/출력 형식/프리셋을 지정하세요. actions 명령으로 확인할 수 있습니다.")
        recipe = Preset("cli", ALL_ACTIONS[action].label, action)
    if output:
        base = recipe.output or preferences.output
        recipe = replace(recipe, output=replace(base, location="folder", directory=str(Path(output).resolve())))
    elif recipe.output is None:
        recipe = replace(recipe, output=preferences.output)
    return recipe


def parser():
    p = argparse.ArgumentParser(prog="magic_converter-cli", description="파일 변환·감시 폴더·실행 중 앱 제어")
    sub = p.add_subparsers(dest="command", required=True)
    for name in sorted(COMMANDS):
        item = sub.add_parser(name)
        item.add_argument("--data-dir", help="설정·IPC·감시 이력을 분리할 폴더")
        item.add_argument("--report", help="UTF-8 JSON 결과 파일 (GUI EXE에서도 사용 가능)")
        item.add_argument("--json", action="store_true", help="표준 출력에 JSON 결과 표시")
        if name in {"convert", "watch", "send"}:
            options = item.add_mutually_exclusive_group(required=True)
            options.add_argument("--action")
            options.add_argument("--preset")
            options.add_argument("--to")
            item.add_argument("--output")
            item.add_argument("paths", nargs="+" if name != "watch" else 1)
        if name in {"convert", "watch", "serve"}:
            item.add_argument("--timeout", type=float, default=3600, help="파일별 실행 제한(초)")
        if name == "serve":
            item.add_argument("--duration", type=float, help="지정 초 후 서버 종료")
        if name == "watch":
            item.add_argument("--recursive", action="store_true")
            item.add_argument("--include", default="*")
            item.add_argument("--settle", type=float, default=2)
            item.add_argument("--once", action="store_true", help="기존 안정화 파일 한 차례 처리 후 종료")
            item.add_argument("--duration", type=float, help="지정 초 후 감시 중지 (진행 작업 취소)")
            item.add_argument("--retry", action="store_true", help="이전 실패·취소 항목 재시도")
        if name == "send":
            item.add_argument("--request-id", default=uuid.uuid4().hex)
        if name in {"cancel", "stop"}:
            if name == "cancel":
                item.add_argument("job_id", type=int, nargs="?")
            item.add_argument("--instance", required=True, help="status 응답의 인스턴스 ID")
        if name == "shell":
            item.add_argument("operation", choices=("install", "remove", "status"))
    return p


def run(argv):
    if sys.stdout is not None and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    command_parser = parser()
    args = command_parser.parse_args(argv)
    if args.command in {"convert", "watch", "serve"} and (not math.isfinite(args.timeout) or args.timeout <= 0):
        command_parser.error("시간 제한은 유한한 양수로 지정하세요.")
    if args.command in {"watch", "serve"} and args.duration is not None and (not math.isfinite(args.duration) or args.duration <= 0):
        command_parser.error("감시/서버 실행 시간은 유한한 양수로 지정하세요.")
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication(["magic_converter"])
    app.setApplicationName("magic_converter")
    app.setOrganizationName("magic_converter")
    app.setQuitOnLastWindowClosed(False)
    directory = data_directory(args.data_dir)
    if args.data_dir:
        os.environ["MAGIC_CONVERTER_PROFILE_DIR"] = str(directory)
    directory.mkdir(parents=True, exist_ok=True)
    code, report = 0, {}
    try:
        preferences = load_preferences(directory)
        if os.environ.get("MAGIC_CONVERTER_PROFILE_DIR"):
            from PySide6.QtCore import QSettings
            profile_settings = QSettings(str(directory / "legacy.ini"), QSettings.Format.IniFormat)
            for key, value in preferences.base_values().items():
                profile_settings.setValue(key, value)
            profile_settings.sync()
        from magic_converter.core.conversion.builtin_converters import runtime_catalog
        from magic_converter.core.conversion.conversion_service import ConversionService
        service = ConversionService(ffmpeg_path=preferences.ffmpeg_path,
                                    file_timeout=getattr(args, "timeout", 3600))
        service.preferences = preferences
        catalog = runtime_catalog(service.ffmpeg)
        if args.command in {"status", "cancel", "stop", "send"}:
            from magic_converter.core.automation.local_ipc import call
            request = {"command": args.command}
            if args.command == "cancel":
                request.update(job_id=args.job_id, instance=args.instance)
            elif args.command == "stop":
                request.update(instance=args.instance)
            if args.command == "send":
                paths = tuple(Path(path).resolve() for path in args.paths)
                recipe = select_recipe(paths, preferences, action=args.action, preset=args.preset, to=args.to,
                                       output=args.output, catalog=catalog)
                request.update(id=args.request_id, recipe=asdict(recipe), paths=[str(path) for path in paths])
            report = call(directory, request)
            code = 0 if report.get("ok") else 1
        elif args.command == "shell":
            from magic_converter.core.automation import explorer_integration as shell
            if args.operation == "install":
                shell.install()
            elif args.operation == "remove":
                shell.remove()
            report = {"ok": True, "command": shell.installed_command()}
        elif args.command == "formats":
            from magic_converter.core.conversion.formats import FORMATS
            report = {"ok": True, "formats": [asdict(item) for item in FORMATS.known]}
        elif args.command == "actions":
            from magic_converter.core.conversion.file_router import ALL_ACTIONS
            report = {"ok": True, "actions": [asdict(item) for item in ALL_ACTIONS.values()]}
        elif args.command == "presets":
            report = {"ok": True, "presets": [asdict(item) for item in preferences.presets]}
        else:
            with redirect_stdout(sys.stderr if sys.stderr is not None else io.StringIO()):
                report, code = execute(app, service, directory, preferences, catalog, args)
    except Exception as exc:
        report, code = {"ok": False, "error": str(exc)}, 1
    if args.report:
        from magic_converter.core.settings.json_store import write_json
        try:
            write_json(Path(args.report).resolve(), report)
        except OSError as exc:
            report, code = {**report, "ok": False, "report_error": str(exc)}, 1
    if sys.stdout is not None:
        print(json.dumps(report, ensure_ascii=False, indent=2), flush=True)
    return code


def execute(app, service, directory, preferences, catalog, args):
    from PySide6.QtCore import QTimer
    from magic_converter.core.conversion.conversion_request import DropSelection
    from magic_converter.core.settings.history import HistoryStore
    from magic_converter.core.automation.automation_store import AutomationStore, WatchRule
    from magic_converter.core.automation.watch_controller import WatchController
    from magic_converter.core.automation.web_inputs import from_url
    from magic_converter.core.settings.preferences import OutputRule
    history = HistoryStore(directory / "history.json", preferences.history_limit)
    results, errors, interrupted = [], [], [False]
    watcher = None
    server, lock, stopping = None, None, [False]
    def record(result):
        history.append(result)
        results.append(result_dict(result))
    def finished(result):
        if watcher and watcher.handle_result(result):
            return
        if args.command == "convert":
            from magic_converter.core.jobs.phase_trace import mark
            mark("conversion_complete")
        record(result)
    service.completed.connect(finished)
    service.failed.connect(errors.append)
    if args.command == "convert":
        remote = tuple(from_url(path) for path in args.paths if path.startswith(("http:", "https:", "data:")))
        if remote and len(remote) != len(args.paths):
            raise ValueError("웹 주소와 로컬 파일은 별도 요청으로 변환하세요.")
        paths = tuple(item.placeholder for item in remote) if remote else tuple(Path(path).resolve() for path in args.paths)
        recipe = select_recipe(paths, preferences, action=args.action, preset=args.preset, to=args.to,
                               output=args.output, catalog=catalog)
        from magic_converter.core.jobs.phase_trace import mark
        mark("conversion_start")
        service.submit_selection(DropSelection(paths, recipe.action_key, recipe, preferences.output, remote))
    elif args.command == "watch":
        root = Path(args.paths[0]).resolve()
        if not root.is_dir():
            raise ValueError("감시 폴더가 존재하지 않습니다.")
        target = ""
        if args.to:
            from magic_converter.core.conversion.formats import FORMATS
            from magic_converter.core.specialized.special_formats import target_action
            detected = FORMATS.detect("file." + args.to.lower().lstrip("."))
            if detected is None or not detected.writable:
                raise ValueError("지원하는 감시 출력 형식을 지정하세요.")
            target = detected.id
            args.action = target_action((), target, catalog)
        recipe = select_recipe((), preferences, action=args.action, preset=args.preset,
                               output=args.output, catalog=catalog)
        output = str(Path(args.output).resolve()) if args.output else str(root / "_converted")
        rule = WatchRule("cli", str(root), output, recipe, args.recursive, args.include, settle=args.settle, target=target)
        store = AutomationStore(directory)
        store.rules = (rule,)
        watcher = WatchController(service, store)
        watcher.completed.connect(record)
        watcher.error.connect(errors.append)
        if args.retry:
            watcher.retry_failed()
        watcher.start()
    else:
        from PySide6.QtCore import QLockFile
        from magic_converter.core.automation.local_ipc import IpcServer
        from magic_converter.core.automation.application_api import ApplicationApi
        lock = QLockFile(str(directory / "magic_converter.lock"))
        lock.setStaleLockTime(0)
        if not lock.tryLock(0):
            raise OSError("이 설정 폴더의 인스턴스가 이미 실행 중입니다.")
        store = AutomationStore(directory)
        if any(rule.enabled for rule in store.rules):
            watcher = WatchController(service, store)
            watcher.completed.connect(record)
            watcher.error.connect(errors.append)
        def stop():
            stopping[0] = True
            if watcher:
                watcher.stop()
            service.cancel_all()
        api = ApplicationApi(service, lambda: preferences, history, watch=watcher, stop=stop)
        api.closing = lambda: stopping[0]
        server = IpcServer(directory, api.handle)
        api.instance = server.instance
        server.start()
        if watcher:
            watcher.start()
    started = time.monotonic()
    def cancel(*_):
        interrupted[0] = True
        if watcher:
            watcher.stop()
        service.cancel_all()
    previous = signal.signal(signal.SIGINT, cancel)
    previous_term = signal.signal(signal.SIGTERM, cancel)
    def tick():
        if args.command == "serve":
            if args.duration is not None and time.monotonic() - started >= args.duration:
                stopping[0] = True
                if watcher:
                    watcher.stop()
                service.cancel_all()
            if (stopping[0] or interrupted[0]) and not service.active_count and not (watcher and watcher.active_count):
                app.quit()
            return
        if watcher:
            expired = args.duration is not None and time.monotonic() - started >= args.duration
            once = args.once and watcher.drained and time.monotonic() - started > args.settle + 2 and not service.active_count
            if expired or once:
                watcher.stop(cancel=expired)
            if not watcher.running and not service.active_count and not watcher.active_count:
                app.quit()
        elif not service.active_count:
            app.quit()
    timer = QTimer()
    timer.setInterval(100)
    timer.timeout.connect(tick)
    timer.start()
    try:
        app.exec()
    finally:
        signal.signal(signal.SIGINT, previous)
        signal.signal(signal.SIGTERM, previous_term)
        timer.stop()
        if watcher:
            watcher.stop()
            watcher.pool.waitForDone()
        service.pool.waitForDone()
        service.office_pool.waitForDone()
        if server:
            server.close()
        if lock:
            lock.unlock()
    ok = not errors and all(not result["errors"] and not result["cancelled"] and not result["timed_out"] for result in results)
    return {"ok": ok and not interrupted[0], "interrupted": interrupted[0], "jobs": results, "errors": errors}, 130 if interrupted[0] else 0 if ok else 1
