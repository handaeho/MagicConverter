"""GUI-thread scheduler for independent files, engine budgets and batch results."""

from __future__ import annotations

import os
import time
from datetime import datetime, timezone
from collections import Counter, deque
from dataclasses import dataclass, field, replace

from PySide6.QtCore import QObject, QThreadPool, Signal, Slot
from PySide6.QtWidgets import QApplication

from magic_converter.core.conversion.conversion_request import ConversionRequest, DropSelection, RequestError, prepare_request
from magic_converter.core.jobs.job_models import FileResult, JobProgress, JobResult, JobSnapshot
from magic_converter.core.settings.preferences import Preferences, default_preferences


@dataclass
class _Task:
    id: int
    job_id: int
    indices: tuple[int, ...]
    worker: object
    resources: tuple[str, ...]
    office: bool
    running: bool = False


@dataclass
class _Batch:
    request: ConversionRequest
    files: list[FileResult]
    percents: list[int | None]
    tasks: set[int] = field(default_factory=set)
    last_percent: int = 0
    started_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    started_clock: float = field(default_factory=time.monotonic)


class ConversionService(QObject):
    completed = Signal(object)
    busy_changed = Signal(int)
    failed = Signal(str)
    progress_changed = Signal(object)
    file_updated = Signal(int, int, object)

    def __init__(self, parent: QObject | None = None, ffmpeg_path: str = "", *,
                 parallelism: int = 2, file_timeout: float | None = 3600) -> None:
        super().__init__(parent)
        if parallelism < 1 or file_timeout is not None and file_timeout <= 0:
            raise ValueError("Parallelism and file timeout must be positive")
        self.ffmpeg_path, self.file_timeout = ffmpeg_path, file_timeout
        self.pool, self.office_pool = QThreadPool(self), QThreadPool(self)
        self.pool.setMaxThreadCount(parallelism)
        self.office_pool.setMaxThreadCount(1)
        self._parallelism = parallelism
        self._running = Counter()
        self._used = Counter()
        self._limits = {"office": 1}
        self._jobs: dict[int, _Batch] = {}
        self._tasks: dict[int, _Task] = {}
        self._pending: deque[int] = deque()
        self._next_id = self._next_task = 0
        self._dispatching = False
        self._last_job: int | None = None
        self.preferences = default_preferences()

    @property
    def active_count(self) -> int:
        return len(self._jobs)

    @property
    def active_file_count(self) -> int:
        return sum(file.state in {"queued", "running"} for job in self._jobs.values() for file in job.files)

    @property
    def ffmpeg(self) -> str | None:
        from magic_converter.core.conversion.converter import find_ffmpeg
        return find_ffmpeg(self.ffmpeg_path)

    def file_results(self, job_id: int) -> tuple[FileResult, ...]:
        job = self._jobs.get(job_id)
        return tuple(job.files) if job is not None else ()

    def job_snapshots(self) -> tuple[JobSnapshot, ...]:
        return tuple(JobSnapshot(job_id, job.request.action, tuple(job.files))
                     for job_id, job in self._jobs.items())

    @Slot(object, str)
    def submit(self, paths: object, key: str) -> None:
        self.submit_selection(DropSelection(tuple(paths), key, output=self.preferences.output))

    @Slot(object)
    def submit_selection(self, selection: DropSelection) -> int | None:
        from magic_converter.core.conversion.builtin_converters import runtime_catalog
        from magic_converter.core.conversion.converter import ConversionWorker
        ffmpeg = self.ffmpeg
        key = selection.action_key
        try:
            request = prepare_request(selection.paths, key, runtime_catalog(ffmpeg),
                                      preset=selection.preset, output=selection.output)
            if selection.web_inputs:
                if tuple(item.placeholder for item in selection.web_inputs) != request.paths:
                    raise RequestError("웹 드래그 항목이 표시된 파일 목록과 일치하지 않습니다.")
                if request.combined or key in {"copy_path", "terminal"}:
                    raise RequestError("웹 입력은 파일별 변환 작업을 선택하세요.")
                output = request.output
                if output.location == "source":
                    from PySide6.QtCore import QStandardPaths
                    from pathlib import Path
                    root = (Path(selection.output.directory) if selection.output.location == "folder" else
                            Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.DownloadLocation)) / "MagicConverter")
                    output = replace(output, location="folder", directory=str(root))
                request = replace(request, output=output, web_inputs=selection.web_inputs)
        except RequestError as exc:
            self.failed.emit(str(exc))
            return None
        return self.submit_request(request, ffmpeg)

    def submit_request(self, request: ConversionRequest, ffmpeg=None) -> int:
        from magic_converter.core.conversion.converter import ConversionWorker
        key = request.action.key
        ffmpeg = self.ffmpeg if ffmpeg is None else ffmpeg
        self._next_id += 1
        job_id = self._next_id
        if key == "copy_path":
            QApplication.clipboard().setText("\n".join(str(path) for path in request.paths))
            self.completed.emit(JobResult(job_id, request.action, len(request.paths), completed=len(request.paths),
                                          files=[FileResult(path, "done") for path in request.paths], request=request,
                                          started_at=datetime.now(timezone.utc).isoformat()))
            return job_id
        job = _Batch(request, [FileResult(path, "queued") for path in request.paths], [0] * len(request.paths))
        self._jobs[job_id] = job
        groups = (tuple(range(len(request.files))),) if request.combined else (
            (index,) for index in range(len(request.files)))
        for indices in groups:
            selected = replace(request, files=tuple(request.files[index] for index in indices))
            self._next_task += 1
            worker = ConversionWorker(self._next_task, selected.paths, request.action, ffmpeg,
                                      request=selected, timeout=self.file_timeout)
            resources = set()
            for planned in selected.files:
                for edge in planned.route.steps:
                    resource = "engine:" + edge.engine_id
                    resources.add(resource)
                    limit = selected.converter(edge.engine_id).max_parallelism
                    limit = max(1, os.cpu_count() or 1) if limit == 0 else max(1, limit)
                    self._limits[resource] = min(self._limits.get(resource, limit), limit)
            if selected.uses_office_queue:
                resources.add("office")
            task = _Task(self._next_task, job_id, indices, worker, tuple(sorted(resources)), selected.uses_office_queue)
            worker.signals.finished.connect(self._on_finished)
            worker.signals.progress.connect(self._on_progress)
            self._tasks[task.id] = task
            job.tasks.add(task.id)
            self._pending.append(task.id)
        self.busy_changed.emit(self.active_count)
        self._publish_progress(job_id, 0, "대기 중")
        self._dispatch()
        return job_id

    def _dispatch(self) -> None:
        if self._dispatching:
            return
        self._dispatching = True
        try:
            # A large batch must yield the next free slot to another ready batch.
            next_job = next((index for index, task_id in enumerate(self._pending)
                             if task_id in self._tasks and self._tasks[task_id].job_id != self._last_job), None)
            if next_job is not None:
                self._pending.rotate(-next_job)
            for _ in range(len(self._pending)):
                task_id = self._pending.popleft()
                task = self._tasks.get(task_id)
                if task is None:
                    continue
                capacity = 1 if task.office else self._parallelism
                if task.worker.cancel_event.is_set() or self._running[task.office] >= capacity or any(
                        self._used[resource] >= self._limits[resource] for resource in task.resources):
                    self._pending.append(task_id)
                    continue
                # Reserve the entire route atomically. No worker occupies a pool
                # thread while waiting for an engine held by another route.
                task.running = True
                self._last_job = task.job_id
                self._running[task.office] += 1
                self._used.update(task.resources)
                job = self._jobs[task.job_id]
                for index in task.indices:
                    job.files[index] = FileResult(job.files[index].source, "running")
                    job.percents[index] = None
                    self.file_updated.emit(task.job_id, index + 1, job.files[index])
                self._publish_progress(task.job_id, task.indices[0], "파일 분석 중")
                (self.office_pool if task.office else self.pool).start(task.worker)
        finally:
            self._dispatching = False

    def _publish_progress(self, job_id: int, index: int, phase: str) -> None:
        job = self._jobs.get(job_id)
        if job is None:
            return
        settled = sum(file.state not in {"queued", "running"} for file in job.files)
        running = sum(file.state == "running" for file in job.files)
        unknown = any(file.state == "running" and percent is None for file, percent in zip(job.files, job.percents))
        percent = None
        if not unknown:
            value = sum(100 if file.state not in {"queued", "running"} else (amount or 0)
                        for file, amount in zip(job.files, job.percents)) // len(job.files)
            job.last_percent = max(job.last_percent, min(100 if settled == len(job.files) else 99, value))
            percent = job.last_percent
        self.progress_changed.emit(JobProgress(job_id, job.request.action.label, job.files[index].source.name,
                                               index + 1, len(job.files), job.percents[index], phase,
                                               aggregate=True, batch_percent=percent,
                                               finished_files=settled, running_files=running))

    @Slot(object)
    def _on_progress(self, progress: JobProgress) -> None:
        task = self._tasks.get(progress.job_id)
        if task is None:
            return
        index = task.indices[min(progress.file_index - 1, len(task.indices) - 1)]
        job = self._jobs[task.job_id]
        # A combined operation publishes one result for all its inputs. Its
        # fraction represents that whole task, rather than one arbitrary file.
        for affected in task.indices if len(task.indices) > 1 else (index,):
            previous = job.percents[affected]
            job.percents[affected] = None if progress.percent is None else max(previous or 0, min(99, progress.percent))
        phase = "취소 요청 중" if task.worker.cancel_event.is_set() else progress.phase
        self._publish_progress(task.job_id, index, phase)

    @Slot(object)
    def _on_finished(self, result: JobResult) -> None:
        task = self._tasks.pop(result.job_id, None)
        if task is None:
            return
        job = self._jobs[task.job_id]
        if task.running:
            self._running[task.office] -= 1
            self._used.subtract(task.resources)
        for offset, index in enumerate(task.indices):
            if offset < len(result.files):
                outcome = result.files[offset]
            else:
                state = "cancelled" if result.cancelled else "timed_out" if result.timed_out else "failed" if result.errors else "done"
                outcome = FileResult(job.files[index].source, state, tuple(result.outputs), tuple(result.errors), tuple(result.warnings))
            job.files[index] = outcome
            job.percents[index] = 100
            self.file_updated.emit(task.job_id, index + 1, outcome)
        job.tasks.remove(task.id)
        self._publish_progress(task.job_id, task.indices[-1], "처리 완료" if not job.tasks else "파일 처리 완료")
        if not job.tasks:
            final = JobResult(task.job_id, job.request.action, len(job.files), files=list(job.files), request=job.request,
                              started_at=job.started_at, elapsed_seconds=time.monotonic() - job.started_clock)
            final.outputs = list(dict.fromkeys(output for file in job.files for output in file.outputs))
            final.errors = list(dict.fromkeys(error for file in job.files for error in file.errors))
            final.warnings = list(dict.fromkeys(warning for file in job.files for warning in file.warnings))
            final.completed = sum(file.state == "done" for file in job.files)
            final.cancelled = any(file.state == "cancelled" for file in job.files)
            final.timed_out = any(file.state == "timed_out" for file in job.files)
            del self._jobs[task.job_id]
            self.completed.emit(final)
            self.busy_changed.emit(self.active_count)
        self._dispatch()

    def _cancel_tasks(self, tasks: list[_Task]) -> None:
        for task in tasks:
            task.worker.cancel_event.set()
        for task in tasks:
            if task.id not in self._tasks:
                continue
            if task.running:
                job = self._jobs[task.job_id]
                for index in task.indices:
                    job.files[index] = replace(job.files[index], cancel_requested=True)
                    self.file_updated.emit(task.job_id, index + 1, job.files[index])
                self._publish_progress(task.job_id, task.indices[0], "취소 요청 중")
            else:
                job = self._jobs[task.job_id]
                self._on_finished(JobResult(task.id, job.request.action, len(task.indices), cancelled=True))

    @Slot()
    def cancel_all(self) -> None:
        self._cancel_tasks(list(self._tasks.values()))

    def cancel_job(self, job_id: int) -> bool:
        tasks = [task for task in self._tasks.values() if task.job_id == job_id]
        self._cancel_tasks(tasks)
        return bool(tasks)

    def cancel_file(self, job_id: int, file_index: int) -> bool:
        tasks = [task for task in self._tasks.values() if task.job_id == job_id and file_index - 1 in task.indices]
        self._cancel_tasks(tasks)
        return bool(tasks)
