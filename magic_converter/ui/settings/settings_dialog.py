"""Application settings, preset editing and engine connection status."""

from __future__ import annotations
from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import QCheckBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget
from dataclasses import replace
from pathlib import Path
from magic_converter.core.conversion.converter import find_ffmpeg
from magic_converter import __version__
from magic_converter.core.settings.preferences import Preferences, default_preferences
from magic_converter.core.settings.json_store import read_json, write_json
from magic_converter.ui.settings.preferences_editor import PreferencesEditor
from magic_converter.ui.common.icons import make_icon


class SettingsDialog(QDialog):
    quit_requested = Signal()

    def __init__(self, values: dict, parent: QWidget | None = None, *,
                 preferences: Preferences | None = None, settings_store=None) -> None:
        super().__init__(parent)
        self.setWindowTitle("magic_converter · 실행 상태 및 설정")
        self.setWindowIcon(make_icon())
        self.setMinimumSize(780, 660)
        self.resize(860, 740)
        self.settings_store = settings_store
        outer = QVBoxLayout(self)
        self.tabs = QTabWidget()
        outer.addWidget(self.tabs)
        general = QWidget()
        layout = QVBoxLayout(general)
        self.tabs.addTab(general, "기본")
        self.running_label = QLabel(f"magic_converter {__version__} · 실행 중")
        self.running_label.setStyleSheet("font-size: 17px; font-weight: 600;")
        layout.addWidget(self.running_label)
        introduction = QLabel("• 창 닫기: 트레이 대기\n"
                              "• 트레이 위치: 숨겨진 아이콘(^)")
        introduction.setWordWrap(True)
        layout.addWidget(introduction)
        form = QFormLayout()
        self.enabled = QCheckBox("전역 드래그 감지 사용")
        self.enabled.setChecked(values.get("enabled", True))
        form.addRow(self.enabled)
        self.interval = QSpinBox()
        self.interval.setRange(30, 50)
        self.interval.setSuffix(" ms")
        self.interval.setValue(values.get("interval_ms", 35))
        form.addRow("감지 주기", self.interval)
        self.threshold = QSpinBox()
        self.threshold.setRange(5, 80)
        self.threshold.setSuffix(" px")
        self.threshold.setToolTip("드래그 판정 거리 · 화면 물리 픽셀 기준")
        self.threshold.setValue(values.get("threshold", 15))
        form.addRow("드래그 거리", self.threshold)
        self.release_modifier = QCheckBox("압축 프로그램 호환: 휠 안에서 키를 뗀 뒤 놓기")
        self.release_modifier.setChecked(preferences.release_modifier_inside if preferences else False)
        self.release_modifier.setToolTip("Shift/Ctrl로 휠 표시 → 슬롯 안에서 키 해제 → 마우스 놓기\n"
                                        "Esc 또는 휠 밖으로 이동해 취소 · 임시 파일 합계 32 MiB까지 보존")
        form.addRow(self.release_modifier)
        self.ffmpeg = QLineEdit(values.get("ffmpeg_path", ""))
        self.ffmpeg.setPlaceholderText("자동: 내장 FFmpeg / PATH")
        row = QHBoxLayout()
        row.addWidget(self.ffmpeg)
        browse = QPushButton("찾기…")
        browse.clicked.connect(self._browse)
        row.addWidget(browse)
        form.addRow("FFmpeg", row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.ffmpeg.textChanged.connect(self._update_status)
        form.addRow("검색 결과", self.status)
        self._update_status()
        layout.addLayout(form)
        help_label = QLabel("<b>Shift · 변환 휠</b> · 파일 형식 변환·이미지 압축<br>"
                            "예: JPG·PNG / MP4·GIF / DOCX·PDF<br>"
                            "<b>Ctrl · 액션 휠</b> · 경로 복사·메타데이터 제거<br>"
                            "ZIP 압축·선택 파일의 폴더에서 터미널 열기<br>"
                            "• 실행: 파일 드래그 중 키 유지 → 슬롯에 파일 놓기<br>"
                            "• 취소: 드롭 전 키 해제·Esc · 두 키 동시: Ctrl 우선<br>"
                            "• 호환 옵션 선택 시: 슬롯 안에서 키 해제 후 놓기 · Esc/휠 밖 이동으로 취소")
        help_label.setTextFormat(Qt.TextFormat.RichText)
        help_label.setWordWrap(True)
        layout.addWidget(help_label)
        formats = QPushButton("지원 형식·제한사항…")
        formats.clicked.connect(self._show_formats)
        layout.addWidget(formats)
        self.editor = PreferencesEditor(self.tabs, preferences or default_preferences(**{
            key: value for key, value in values.items() if key in Preferences().base_values()}))
        transfer = QHBoxLayout()
        for label, callback in (("설정 내보내기…", self._export), ("설정 가져오기…", self._import),
                                ("백업 복구", self._restore), ("기본값 복원", self._reset)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            transfer.addWidget(button)
            if label == "백업 복구":
                button.setEnabled(settings_store is not None and settings_store.backup.is_file())
        outer.addLayout(transfer)
        self.transfer_status = QLabel("가져오기·복구·기본값 복원은 저장 버튼을 누르면 적용됩니다.")
        self.transfer_status.setWordWrap(True)
        outer.addWidget(self.transfer_status)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Save).setText("저장·트레이 이동")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("트레이 이동")
        quit_button = buttons.addButton("종료", QDialogButtonBox.ButtonRole.ActionRole)
        quit_button.clicked.connect(self.quit_requested.emit)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def accept(self):
        try:
            self.values()
        except ValueError as exc:
            QMessageBox.warning(self, "설정 확인", str(exc))
            return
        super().accept()

    def _load_preferences(self, preferences):
        self.enabled.setChecked(preferences.enabled)
        self.interval.setValue(preferences.interval_ms)
        self.threshold.setValue(preferences.threshold)
        self.release_modifier.setChecked(preferences.release_modifier_inside)
        self.ffmpeg.setText(preferences.ffmpeg_path)
        self.editor.set_preferences(preferences)
        self.transfer_status.setText("설정을 불러왔습니다. 저장하면 적용되며, 트레이 이동을 누르면 취소됩니다.")

    def _export(self):
        try:
            preferences = self.values()["preferences"]
            filename, _ = QFileDialog.getSaveFileName(self, "설정 내보내기", "magic-converter-settings.json", "JSON (*.json)")
            if filename:
                write_json(Path(filename), preferences.to_dict())
                self.transfer_status.setText("설정을 내보냈습니다. 결과 기록은 포함하지 않습니다.")
        except (OSError, ValueError) as exc:
            QMessageBox.warning(self, "내보내기 실패", str(exc))

    def _import(self):
        filename, _ = QFileDialog.getOpenFileName(self, "설정 가져오기", "", "JSON (*.json)")
        if filename:
            try:
                self._load_preferences(Preferences.from_dict(read_json(Path(filename))))
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "가져오기 실패", str(exc))

    def _restore(self):
        if self.settings_store is not None:
            try:
                self._load_preferences(self.settings_store.read_backup())
            except (OSError, ValueError) as exc:
                QMessageBox.warning(self, "복구 실패", str(exc))

    def _reset(self):
        self._load_preferences(default_preferences())

    def _browse(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(self, "FFmpeg 실행 파일", self.ffmpeg.text(),
                                                 "FFmpeg (ffmpeg.exe);;실행 파일 (*.exe)")
        if filename:
            self.ffmpeg.setText(filename)

    def _show_formats(self) -> None:
        from magic_converter.ui.help.format_help import FormatHelpDialog
        FormatHelpDialog(self).exec()

    def _update_status(self) -> None:
        executable = find_ffmpeg(self.ffmpeg.text())
        self.status.setText("사용 가능" if executable else "미발견 · 영상·오디오 변환 제한")
        self.status.setToolTip(str(executable) if executable else "이미지·ZIP·경로 복사 사용 가능")

    def values(self) -> dict:
        base = {"enabled": self.enabled.isChecked(), "interval_ms": self.interval.value(),
                "threshold": self.threshold.value(), "ffmpeg_path": self.ffmpeg.text().strip()}
        return {**base, "preferences": replace(self.editor.value(), **base,
                                                release_modifier_inside=self.release_modifier.isChecked())}
