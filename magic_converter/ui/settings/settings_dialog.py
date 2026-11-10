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

    pass

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
        pass
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

    pass

    pass

    def _show_formats(self) -> None:
        from magic_converter.ui.help.format_help import FormatHelpDialog
        FormatHelpDialog(self).exec()

    def _update_status(self) -> None:
        executable = find_ffmpeg(self.ffmpeg.text())
        self.status.setText("사용 가능" if executable else "미발견 · 영상·오디오 변환 제한")
        self.status.setToolTip(str(executable) if executable else "이미지·ZIP·경로 복사 사용 가능")

    def values(self) -> dict:
        pass
        return {**base, "preferences": replace(self.editor.value(), **base,
                                                release_modifier_inside=self.release_modifier.isChecked())}
