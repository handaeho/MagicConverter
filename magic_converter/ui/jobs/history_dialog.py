"""Inspect persistent per-file outcomes and open existing output folders."""

from pathlib import Path

from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QLabel,
                               QMessageBox, QPushButton, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from magic_converter.ui.common.icons import make_icon


class HistoryDialog(QDialog):
    def __init__(self, store):
        super().__init__()
        self.store = store
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle("magic_converter · 결과 기록")
        self.setWindowIcon(make_icon())
        self.resize(880, 600)
        layout = QVBoxLayout(self)
        self.summary = QLabel()
        layout.addWidget(self.summary)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["완료 시각 / 작업 / 파일", "결과", "출력"])
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        self.tree.itemSelectionChanged.connect(self._selected)
        layout.addWidget(self.tree, 2)
        self.detail = QTextEdit()
        self.detail.setReadOnly(True)
        layout.addWidget(self.detail, 1)
        row = QHBoxLayout()
        self.open_button = QPushButton("출력 폴더 열기")
        self.open_button.clicked.connect(self._open)
        row.addWidget(self.open_button)
        self.clear_button = QPushButton("기록 지우기")
        self.clear_button.clicked.connect(self._clear)
        row.addWidget(self.clear_button)
        row.addStretch()
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.button(QDialogButtonBox.StandardButton.Close).setText("닫기")
        close.rejected.connect(self.reject)
        row.addWidget(close)
        layout.addLayout(row)
        self.refresh()

    def refresh(self, *_):
        selected = self.tree.currentItem()
        identity = selected.data(0, Qt.ItemDataRole.UserRole)[0] if selected is not None else None
        self.tree.clear()
        labels = {"done": "완료", "failed": "실패", "cancelled": "취소됨", "timed_out": "시간 초과"}
        for entry in reversed(self.store.entries):
            files = entry["files"]
            done = sum(file["state"] == "done" for file in files)
            stamp = str(entry.get("finished_at", ""))
            try:
                from datetime import datetime
                stamp = datetime.fromisoformat(stamp).astimezone().strftime("%m-%d %H:%M:%S")
            except ValueError:
                pass
            root = QTreeWidgetItem(self.tree, [f"{stamp} · {entry.get('label', '')}", f"{done}/{len(files)}개 완료", ""])
            paths = [path for file in files for path in file["outputs"]]
            options, output = entry.get("options", {}), entry.get("output", {})
            detail = [f"작업: {entry.get('label', '')}", f"완료: {stamp}",
                      f"소요: {entry.get('elapsed_seconds', 0)}초", f"처리: {done}/{len(files)}개 완료"]
            if output:
                detail.append("저장 위치: " + ("원본 폴더" if output.get("location") == "source" else str(output.get("directory", ""))))
                detail.append("출력 이름: " + str(output.get("template", "")))
            if options and str(entry.get("action_key", "")).startswith("image:"):
                detail.append(f"이미지 품질: {options.get('image_quality', 90)} · 긴 변: {options.get('image_max_edge') or '원본'}")
            if options.get("video_crf") is not None:
                detail.append(f"영상 CRF: {options['video_crf']}")
            if options.get("audio_bitrate") is not None:
                detail.append(f"오디오: {options['audio_bitrate']} kbps")
            if entry.get("tools") and str(entry.get("action_key", "")).startswith("tool:"):
                from magic_converter.core.settings.preferences import ToolOptions
                from magic_converter.core.actions.tool_definitions import TOOL_BY_KEY, tool_summary
                if entry["action_key"] in TOOL_BY_KEY:
                    try:
                        detail.append(tool_summary(entry["action_key"], ToolOptions(**entry["tools"])))
                    except (TypeError, ValueError):
                        detail.append("도구 옵션을 읽지 못했습니다.")
            root.setData(0, Qt.ItemDataRole.UserRole, ((entry.get("id"), -1), paths, "\n".join(detail)))
            for index, file in enumerate(files):
                row = QTreeWidgetItem(root, [Path(file["source"]).name, labels[file["state"]], "\n".join(file["outputs"])])
                detail = "\n".join([file["source"], *file["outputs"], *file["errors"], *file["warnings"]])
                row.setData(0, Qt.ItemDataRole.UserRole, ((entry.get("id"), index), file["outputs"], detail))
                row.setToolTip(0, file["source"])
                if identity == (entry.get("id"), index):
                    row.setSelected(True)
                    self.tree.setCurrentItem(row)
                    root.setExpanded(True)
            if identity == (entry.get("id"), -1):
                self.tree.setCurrentItem(root)
        self.summary.setText(f"최근 작업 {len(self.store.entries)}개 · 기록을 지워도 출력 파일은 유지됩니다.")
        self.clear_button.setEnabled(bool(self.store.entries))
        self._selected()

    def _selected(self):
        item = self.tree.currentItem()
        data = item.data(0, Qt.ItemDataRole.UserRole) if item is not None else None
        self.detail.setPlainText(data[2] if data else "작업 또는 파일을 선택하면 경로·오류·경고를 확인할 수 있습니다.")
        self.open_button.setEnabled(bool(data and any(Path(path).exists() for path in data[1])))

    def _open(self):
        item = self.tree.currentItem()
        if item is not None:
            for value in item.data(0, Qt.ItemDataRole.UserRole)[1]:
                path = Path(value)
                if path.exists():
                    QDesktopServices.openUrl(QUrl.fromLocalFile(str(path if path.is_dir() else path.parent)))
                    return

    def _clear(self):
        if QMessageBox.question(self, "결과 기록 지우기", "저장된 결과 기록을 모두 지울까요? 출력 파일은 유지됩니다.") != QMessageBox.StandardButton.Yes:
            return
        try:
            self.store.clear()
            self.refresh()
        except OSError as exc:
            QMessageBox.warning(self, "기록 저장 실패", str(exc))
