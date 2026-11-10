"""Active batch/file status and selective cancellation from the system tray."""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Slot
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView,
                               QLabel, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from magic_converter.core.conversion.conversion_service import ConversionService
from magic_converter.core.jobs.job_models import JobProgress
from magic_converter.ui.common.icons import make_icon


class JobManagerDialog(QDialog):
    def __init__(self, service: ConversionService) -> None:
        super().__init__()
        self.service = service
        self.setWindowTitle("magic_converter · 작업 관리")
        self.setWindowIcon(make_icon())
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setMinimumSize(700, 360)
        self.resize(820, 460)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["작업 / 파일", "상태", "원본 폴더"])
        self.tree.setSelectionMode(QTreeWidget.SelectionMode.SingleSelection)
        self.tree.setRootIsDecorated(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.itemSelectionChanged.connect(self._update_buttons)
        layout.addWidget(self.tree)
        actions = QHBoxLayout()
        self.cancel_file_button = QPushButton("선택 파일 취소")
        self.cancel_job_button = QPushButton("선택 작업 취소")
        self.cancel_all_button = QPushButton("모두 취소")
        self.cancel_file_button.clicked.connect(self._cancel_file)
        self.cancel_job_button.clicked.connect(self._cancel_job)
        self.cancel_all_button.clicked.connect(service.cancel_all)
        for button in (self.cancel_file_button, self.cancel_job_button, self.cancel_all_button):
            actions.addWidget(button)
        actions.addStretch()
        layout.addLayout(actions)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        layout.addWidget(close)
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.setInterval(100)
        self._refresh_timer.timeout.connect(self.refresh)
        self._percents: dict[tuple[int, int], int | None] = {}
        self._batch_percents: dict[int, int | None] = {}
        service.file_updated.connect(self._schedule_refresh)
        service.busy_changed.connect(self._schedule_refresh)
        service.progress_changed.connect(self._progress)
        self.refresh()

    def _schedule_refresh(self, *_) -> None:
        if not self._refresh_timer.isActive():
            self._refresh_timer.start()

    @Slot(object)
    def _progress(self, progress: JobProgress) -> None:
        self._percents[progress.job_id, progress.file_index] = progress.percent
        self._batch_percents[progress.job_id] = progress.overall_percent
        self._schedule_refresh()

    def _selection(self):
        selected = self.tree.selectedItems()
        return selected[0].data(0, Qt.ItemDataRole.UserRole) if selected else None

    def refresh(self) -> None:
        selection = self._selection()
        scroll = self.tree.verticalScrollBar().value()
        expansion = {self.tree.topLevelItem(i).data(0, Qt.ItemDataRole.UserRole)[0]:
                     self.tree.topLevelItem(i).isExpanded() for i in range(self.tree.topLevelItemCount())}
        snapshots = self.service.job_snapshots()
        self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        labels = {"queued": "대기 중", "running": "변환 중", "done": "완료", "failed": "실패",
                  "cancelled": "취소됨", "timed_out": "시간 초과"}
        for job in snapshots:
            settled = sum(file.state not in {"queued", "running"} for file in job.files)
            percent = self._batch_percents.get(job.job_id)
            status = f"{settled}/{len(job.files)}개 처리" + (f" · {percent}%" if percent is not None else "")
            root = QTreeWidgetItem(self.tree, [f"{job.action.label} · 작업 {job.job_id}", status, ""])
            root.setData(0, Qt.ItemDataRole.UserRole, (job.job_id, 0))
            for index, file in enumerate(job.files, start=1):
                percent = self._percents.get((job.job_id, index))
                status = "취소 요청 중" if file.cancel_requested else labels[file.state]
                if file.state == "running" and not file.cancel_requested and percent is not None:
                    status += f" · {percent}%"
                row = QTreeWidgetItem(root, [file.source.name, status, str(file.source.parent)])
                key = (job.job_id, index)
                row.setData(0, Qt.ItemDataRole.UserRole, key)
                row.setToolTip(0, str(file.source))
                row.setToolTip(1, "\n".join((*file.errors, *file.warnings)))
                row.setToolTip(2, str(file.source.parent))
                if file.state in {"failed", "timed_out"}:
                    row.setForeground(1, QColor("#c33c3c"))
                if selection is not None and tuple(selection) == key:
                    row.setSelected(True)
            root.setExpanded(expansion.get(job.job_id, True))
            if selection is not None and tuple(selection) == (job.job_id, 0):
                root.setSelected(True)
        active_ids = {job.job_id for job in snapshots}
        self._percents = {key: value for key, value in self._percents.items() if key[0] in active_ids}
        self._batch_percents = {key: value for key, value in self._batch_percents.items() if key in active_ids}
        self.tree.setUpdatesEnabled(True)
        self.tree.verticalScrollBar().setValue(scroll)
        self.summary.setText(f"진행/대기 작업 {len(snapshots)}개 · 파일 {self.service.active_file_count}개"
                             if snapshots else "진행/대기 중인 작업이 없습니다.")
        self._update_buttons()

    def _update_buttons(self) -> None:
        selection = self._selection()
        files = self.service.file_results(selection[0]) if selection is not None else ()
        active = bool(files) and any(file.state in {"queued", "running"} and not file.cancel_requested
                                     for file in files)
        index = selection[1] if selection is not None else 0
        self.cancel_job_button.setEnabled(active)
        self.cancel_file_button.setEnabled(active and 0 < index <= len(files)
                                           and files[index - 1].state in {"queued", "running"}
                                           and not files[index - 1].cancel_requested)
        self.cancel_all_button.setEnabled(any(
            file.state in {"queued", "running"} and not file.cancel_requested
            for job in self.service.job_snapshots() for file in job.files))

    def _cancel_file(self) -> None:
        selection = self._selection()
        if selection is not None and selection[1] > 0:
            self.service.cancel_file(*selection)
            self.refresh()

    def _cancel_job(self) -> None:
        selection = self._selection()
        if selection is not None:
            self.service.cancel_job(selection[0])
            self.refresh()
