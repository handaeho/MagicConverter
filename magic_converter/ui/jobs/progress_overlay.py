"""Nonactivating, click-through conversion progress at the screen corner."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QFrame, QLabel, QProgressBar, QVBoxLayout, QWidget

from magic_converter.core.conversion.converter import JobProgress, JobResult


class ProgressRow(QWidget):
    def __init__(self, parent: QWidget) -> None:
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 4)
        layout.setSpacing(5)
        self.title = QLabel()
        self.detail = QLabel()
        self.detail.setStyleSheet("color: #bdbdc9; font-size: 11px;")
        self.bar = QProgressBar()
        self.bar.setFixedHeight(18)
        self.bar.setRange(0, 100)
        for widget in (self.title, self.detail, self.bar):
            layout.addWidget(widget)

    def set_progress(self, progress: JobProgress) -> None:
        percent = progress.overall_percent
        self.title.setText(f"{progress.action_label} · {progress.finished_files}/{progress.total_files}개 완료"
                           if progress.aggregate else f"{progress.action_label} · {progress.file_index}/{progress.total_files}개")
        filename = self.detail.fontMetrics().elidedText(progress.filename, Qt.TextElideMode.ElideMiddle, 300)
        active = f" · {progress.running_files}개 처리 중" if progress.aggregate and progress.running_files else ""
        self.detail.setText(f"{progress.phase}{active} · {filename}")
        self.bar.setRange(0, 0 if percent is None else 100)
        if percent is not None:
            self.bar.setValue(percent)
            self.bar.setFormat(f"{percent}%")

    def finish(self, result: JobResult) -> None:
        self.bar.setRange(0, 100)
        if result.cancelled:
            self.bar.setValue(0)
            self.bar.setFormat("취소됨")
            self.detail.setText(f"작업 취소 · {result.completed}개 완료")
        elif result.errors:
            self.title.setText(f"{result.action.label} · {result.completed}/{result.total}개 완료")
            self.bar.setFormat("시간 초과" if result.timed_out else "오류")
            self.bar.setStyleSheet("QProgressBar::chunk { background: #dc6767; }")
            self.detail.setText(self.detail.fontMetrics().elidedText(result.errors[0], Qt.TextElideMode.ElideRight, 300))
        else:
            self.title.setText(f"{result.action.label} · 완료")
            self.bar.setValue(100)
            self.bar.setFormat("100% · 완료")
            self.detail.setText(f"{result.completed}개 파일 처리 완료")
            if result.warnings:
                self.title.setText(f"{result.action.label} · 완료 (서식 안내)")
                self.detail.setText(self.detail.fontMetrics().elidedText(result.warnings[0], Qt.TextElideMode.ElideRight, 300))


class ProgressOverlay(QWidget):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus
                            | Qt.WindowType.WindowTransparentForInput)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setFixedWidth(348)
        self.setWindowTitle("magic_converter · 진행률")
        self.setStyleSheet("""
            QFrame#progressPanel { background: rgba(28, 28, 34, 242); border: 1px solid #4a4140; border-radius: 12px; }
            QLabel { color: #f0f0f4; font-family: 'Segoe UI', 'Malgun Gothic'; font-size: 12px; }
            QProgressBar { border: 0; border-radius: 5px; background: #42424b; color: white; text-align: center; font-size: 11px; }
            QProgressBar::chunk { border-radius: 5px; background: #d87912; }
        """)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        panel = QFrame()
        panel.setObjectName("progressPanel")
        self.rows_layout = QVBoxLayout(panel)
        self.rows_layout.setContentsMargins(16, 12, 16, 12)
        self.rows_layout.setSpacing(7)
        heading = QLabel("magic_converter · 작업 진행")
        heading.setStyleSheet("font-weight: 600; color: #ffb65c;")
        self.rows_layout.addWidget(heading)
        self.more = QLabel()
        self.rows_layout.addWidget(self.more)
        self.more.hide()
        outer.addWidget(panel)
        self.rows: dict[int, ProgressRow] = {}
        self._timers: dict[int, QTimer] = {}
        app = QGuiApplication.instance()
        if app is not None:
            app.primaryScreenChanged.connect(self._position)
            for screen in app.screens():
                screen.availableGeometryChanged.connect(self._position)

    @Slot(object)
    def update_progress(self, progress: JobProgress) -> None:
        row = self.rows.get(progress.job_id)
        if row is None:
            row = ProgressRow(self)
            self.rows[progress.job_id] = row
            self.rows_layout.insertWidget(self.rows_layout.count() - 1, row)
        row.set_progress(progress)
        self._refresh()

    @Slot(object)
    def finish_job(self, result: JobResult) -> None:
        row = self.rows.get(result.job_id)
        if row is None:
            return
        row.finish(result)
        timer = QTimer(self)
        timer.setSingleShot(True)
        timer.timeout.connect(lambda: self._remove(result.job_id))
        self._timers[result.job_id] = timer
        timer.start(6000 if result.warnings else 4000 if result.errors else 2000)
        self._refresh()

    def _remove(self, job_id: int) -> None:
        row = self.rows.pop(job_id, None)
        if row is not None:
            row.hide()
            self.rows_layout.removeWidget(row)
            row.deleteLater()
        timer = self._timers.pop(job_id, None)
        if timer is not None:
            timer.deleteLater()
        self._refresh()

    def _refresh(self) -> None:
        # Finished rows yield room to live jobs while their success/error
        # linger timer is running.
        ordered = sorted(self.rows, key=lambda job_id: job_id in self._timers)
        visible = set(ordered[:3])
        for job_id, row in self.rows.items():
            row.setVisible(job_id in visible)
        extra = max(0, len(self.rows) - 3)
        self.more.setText(f"외 {extra}개 작업")
        self.more.setVisible(extra > 0)
        self.rows_layout.invalidate()
        self.rows_layout.activate()
        self.layout().invalidate()
        self.setFixedHeight(self.layout().totalSizeHint().height())
        self.layout().activate()
        self._position()
        if self.rows:
            self.show()
            self.raise_()
        else:
            self.hide()

    def _position(self, *args: object) -> None:
        screen = QGuiApplication.primaryScreen()
        if screen is not None:
            bounds = screen.availableGeometry()
            self.move(bounds.right() - self.width() - 15, bounds.bottom() - self.height() - 15)

    def shutdown(self) -> None:
        for timer in self._timers.values():
            timer.stop()
        self.hide()
