"""Qt bridge: stable scans and publication off-thread, conversion on shared queue."""
from __future__ import annotations

import threading
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QObject, QRunnable, QThreadPool, QTimer, QLockFile, Signal

from magic_converter.core.automation.automation_store import AutomationStore
from magic_converter.core.conversion.builtin_converters import runtime_catalog
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.jobs.job_control import Control, JobCancelled, current
from magic_converter.core.jobs.job_models import FileResult
from magic_converter.core.settings.preferences import OutputRule
from magic_converter.core.automation.watch_engine import WatchEngine


class _Signals(QObject):
    finished = Signal(object, object)


class _Work(QRunnable):
    def __init__(self, function, cancel):
        super().__init__()
        self.function, self.cancel = function, cancel
        self.signals = _Signals()

    def run(self):
        token = current.set(Control(self.cancel, lambda _: None))
        value, error = None, None
        try:
            value = self.function()
        except Exception as exc:
            error = exc
        finally:
            current.reset(token)
        self.signals.finished.emit(value, error)


class WatchController(QObject):
    completed = Signal(object)
    error = Signal(str)
    changed = Signal()

    def __init__(self, service, store: AutomationStore, parent=None):
        super().__init__(parent)
        self.service, self.store = service, store
        self.engine = WatchEngine(store.directory, store.rules)
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)
        self.timer = QTimer(self)
        self.timer.setInterval(500)
        self.timer.timeout.connect(self.scan)
        self.lock = QLockFile(str(store.directory / "watch.lock"))
        self.lock.setStaleLockTime(0)
        self.running, self.scanning = False, False
        self.jobs = {}
        self.workers = set()
        self.stop_event = threading.Event()
        self.initialized = False

    @property
    def active_count(self):
        return len(self.jobs) + len(self.workers)

    @property
    def drained(self):
        # A moment between publication and the next scan is not a completed
        # --once batch. The engine must finish a quiet, stable-input scan.
        return self.initialized and not self.active_count and self.engine.quiescent

    def work(self, function, callback, *, cancellable=True):
        task = _Work(function, self.stop_event if cancellable else threading.Event())
        self.workers.add(task)
        def done(value, error):
            self.workers.discard(task)
            try:
                callback(value, error)
            finally:
                self.release_if_idle()
                self.changed.emit()
        task.signals.finished.connect(done)
        self.pool.start(task)

    def start(self):
        if self.running:
            return
        if self.active_count:
            raise ValueError("이전 감시 작업이 종료 중입니다.")
        if not self.lock.tryLock(0):
            raise OSError("이 설정 폴더의 감시 작업이 다른 프로세스에서 실행 중입니다.")
        self.stop_event.clear()
        self.engine.busy_rules.clear()
        self.engine.pending.clear()
        self.engine.quiescent = False
        self.engine.rules = self.store.rules
        if any(rule.enabled and not Path(rule.folder).is_dir() for rule in self.store.rules):
            self.lock.unlock()
            raise ValueError("사용 중인 감시 폴더가 존재하지 않습니다.")
        self.running = True
        self.work(self.engine.recover, self.recovered)
        self.changed.emit()

    def recovered(self, value, error):
        if error:
            self.error.emit(str(error))
            self.stop()
            return
        self.initialized = True
        if self.running:
            self.timer.start()
            self.scan()

    def scan(self):
        if not self.running or self.scanning or self.workers:
            return
        self.scanning = True
        self.work(self.engine.scan, self.scanned)

    def scanned(self, entries, error):
        self.scanning = False
        if error:
            if not isinstance(error, JobCancelled):
                self.error.emit(str(error))
            return
        for rule, event in entries or ():
            if not self.running:
                self.engine.journal.update(event["key"], "cancelled", "감시 중지")
                self.engine.busy_rules.discard(rule.id)
                self.engine.cleanup(event)
                continue
            try:
                output = OutputRule("folder", str(Path(event["work"]) / "results"), rule.recipe.output.template if rule.recipe.output else "{stem}_{tag}")
                recipe = replace(rule.recipe, output=output)
                catalog = runtime_catalog(self.service.ffmpeg)
                if rule.target:
                    from magic_converter.core.specialized.special_formats import target_action
                    key = target_action([event["source"]], rule.target, catalog)
                    if key is None:
                        raise ValueError("이 파일은 지정한 감시 출력 형식으로 변환할 수 없습니다.")
                    recipe = replace(recipe, action_key=key)
                request = prepare_request([event["source"]], recipe.action_key, catalog, preset=recipe)
                job_id = self.service.submit_request(request)
                self.jobs[job_id] = event
            except Exception as exc:
                self.engine.journal.update(event["key"], "failed", str(exc))
                self.engine.busy_rules.discard(rule.id)
                self.engine.cleanup(event)
                self.error.emit(str(exc))

    def handle_result(self, result):
        event = self.jobs.pop(result.job_id, None)
        if event is None:
            return False
        successful = result.completed == result.total and not result.cancelled and not result.errors
        def publish():
            try:
                return self.engine.finish(event, successful, result.outputs,
                                          "cancelled" if result.cancelled else "\n".join(result.errors))
            finally:
                # An interrupted publish keeps its receipt for recovery.
                if self.engine.journal.get(event["key"])["state"] != "publishing":
                    try:
                        self.engine.cleanup(event)
                    except OSError as exc:
                        result.warnings.append(f"감시 임시 작업 폴더를 정리하지 못했습니다: {exc}")
        def finished(target, error):
            if target is None or error:
                result.outputs = []
                result.completed = 0
                if error:
                    result.cancelled = isinstance(error, JobCancelled)
                    self.engine.journal.update(event["key"], "cancelled" if result.cancelled else "failed", str(error))
                    if not result.cancelled:
                        result.errors.append(str(error))
                elif successful:
                    result.errors.append(self.engine.journal.get(event["key"])["detail"])
                state = "cancelled" if result.cancelled else "failed"
                result.files = [FileResult(Path(event["source"]), state, errors=tuple(result.errors), warnings=tuple(result.warnings))]
            else:
                result.outputs = [target]
                result.files = [replace(file, outputs=(target,)) for file in result.files]
                result.request = replace(result.request, output=OutputRule("folder", str(target.parent)))
            self.completed.emit(result)
        # Publication observes stop too. Cancellation never removes a committed result.
        self.work(publish, finished)
        return True

    def stop(self, cancel=True):
        self.running = False
        self.timer.stop()
        if cancel:
            self.stop_event.set()
            for job_id in tuple(self.jobs):
                self.service.cancel_job(job_id)
        self.release_if_idle()
        self.changed.emit()

    def release_if_idle(self):
        if not self.running and not self.active_count:
            self.lock.unlock()

    def retry_failed(self):
        if self.active_count:
            raise ValueError("진행 중인 감시 작업이 종료된 뒤 재시도하세요.")
        for event in self.engine.journal.rows({"failed", "cancelled", "interrupted"}):
            self.engine.journal.retry(event["key"])
