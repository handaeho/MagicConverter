"""System tray menu, application controls and completion notifications."""

from __future__ import annotations
from PySide6.QtCore import Signal
from PySide6.QtGui import QAction
from PySide6.QtWidgets import QDialog, QMenu, QMessageBox, QSystemTrayIcon
from magic_converter.core.conversion.converter import JobProgress, JobResult
from magic_converter.core.settings.preferences import default_preferences
from magic_converter.ui.common.icons import make_icon
from magic_converter.ui.settings.settings_dialog import SettingsDialog


class TrayIcon(QSystemTrayIcon):
    quit_requested = Signal()
    enabled_changed = Signal(bool)
    settings_changed = Signal(object)
    cancel_requested = Signal()

    def __init__(self, settings: dict, parent: object = None) -> None:
        super().__init__(make_icon(), parent)
        self.values = settings.copy()
        self.setToolTip("magic_converter · Shift: 변환 / Ctrl: 액션")
        self.menu = QMenu()
        title = self.menu.addAction("magic_converter")
        title.setEnabled(False)
        self.status_action = self.menu.addAction("대기 중")
        self.status_action.setEnabled(False)
        self.menu.addSeparator()
        self.enabled_action = QAction("드래그 감지 사용", self.menu)
        self.enabled_action.setCheckable(True)
        self.enabled_action.setChecked(settings.get("enabled", True))
        self.enabled_action.toggled.connect(self._toggle_enabled)
        self.menu.addAction(self.enabled_action)
        self.settings_action = self.menu.addAction("실행 상태 / 설정…", self.show_settings)
        self.jobs_action = self.menu.addAction("작업 관리…", self.show_jobs)
        self.jobs_action.setEnabled(False)
        self.history_action = self.menu.addAction("결과 기록…", self.show_history)
        self.history_action.setEnabled(False)
        self.menu.addAction("사용 방법", self.show_help)
        self.menu.addAction("지원 형식…", self.show_formats)
        self.cancel_action = self.menu.addAction("진행/대기 변환 모두 취소", self.cancel_requested.emit)
        self.cancel_action.setEnabled(False)
        self.menu.addSeparator()
        self.quit_action = self.menu.addAction("종료", self.quit_requested.emit)
        self.setContextMenu(self.menu)
        self.activated.connect(self._activated)
        self._settings_dialog: SettingsDialog | None = None
        self._jobs_dialog = None
        self._conversion_service = None
        self._history_dialog = None
        self._history_store = None
        self._preferences = default_preferences()
        self._settings_store = None

    def _toggle_enabled(self, enabled: bool) -> None:
        self.values["enabled"] = enabled
        self.enabled_changed.emit(enabled)

    def set_conversion_service(self, service) -> None:
        self._conversion_service = service
        self.jobs_action.setEnabled(True)

    def set_preferences(self, preferences, store):
        self._preferences, self._settings_store = preferences, store
        self.values = preferences.base_values()
        self.enabled_action.blockSignals(True)
        self.enabled_action.setChecked(preferences.enabled)
        self.enabled_action.blockSignals(False)

    def set_history(self, store):
        self._history_store = store
        self.history_action.setEnabled(True)

    def show_history(self):
        if self._history_store is None:
            return
        if self._history_dialog is None:
            from magic_converter.ui.jobs.history_dialog import HistoryDialog
            self._history_dialog = HistoryDialog(self._history_store)
            self._history_dialog.finished.connect(self._history_closed)
            if self._conversion_service is not None:
                self._conversion_service.completed.connect(self._history_dialog.refresh)
        self._history_dialog.refresh()
        self._history_dialog.show()
        self._history_dialog.raise_()
        self._history_dialog.activateWindow()

    def _history_closed(self, *_):
        self._history_dialog = None

    def show_jobs(self) -> None:
        if self._conversion_service is None:
            return
        if self._jobs_dialog is None:
            from magic_converter.ui.jobs.job_manager import JobManagerDialog
            self._jobs_dialog = JobManagerDialog(self._conversion_service)
            self._jobs_dialog.finished.connect(self._jobs_closed)
        self._jobs_dialog.show()
        self._jobs_dialog.raise_()
        self._jobs_dialog.activateWindow()

    def _jobs_closed(self, *_):
        self._jobs_dialog = None

    def _activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self.show_settings()

    def show_settings(self) -> None:
        if self._settings_dialog is not None:
            self._settings_dialog.raise_()
            self._settings_dialog.activateWindow()
            return
        dialog = SettingsDialog(self.values,
                                preferences=self._preferences, settings_store=self._settings_store)
        self._settings_dialog = dialog
        dialog.quit_requested.connect(self.quit_requested.emit)

        def finished(result: int) -> None:
            if result == QDialog.DialogCode.Accepted:
                values = dialog.values()
                self.values = {key: values[key] for key in self._preferences.base_values()}
                self.enabled_action.blockSignals(True)
                self.enabled_action.setChecked(self.values["enabled"])
                self.enabled_action.blockSignals(False)
                self.settings_changed.emit(values)
            self._settings_dialog = None
            dialog.deleteLater()

        dialog.finished.connect(finished)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def show_help(self) -> None:
        QMessageBox.information(None, "magic_converter 사용 방법",
                                "1. 탐색기에서 파일을 선택하고 드래그합니다.\n"
                                "2. Shift(변환) 또는 Ctrl(액션)을 누릅니다.\n"
                                "3. 활성 슬롯 위에서 마우스 버튼을 놓습니다.\n\n"
                                "Esc 또는 보조키 해제로 취소합니다.\n"
                                "결과는 출력 설정에 지정한 위치와 이름으로 저장됩니다.\n"
                                "브라우저의 공개 파일 링크·이미지도 드래그할 수 있습니다.\n"
                                "감시 폴더·웹 출력·탐색기 메뉴는 트레이의 자동화 메뉴에서 설정합니다.\n"
                                "50% 압축은 필요하면 해상도도 줄입니다.\n"
                                "영상·오디오는 FFmpeg가 필요합니다.\n"
                                "알림이 보이지 않으면 Windows 알림 설정을 확인하세요.")

    def show_formats(self) -> None:
        from magic_converter.ui.help.format_help import FormatHelpDialog
        FormatHelpDialog().exec()

    def set_busy(self, count: int) -> None:
        self.cancel_action.setEnabled(bool(count))
        self.status_action.setText(f"진행/대기 작업: {count}개" if count else "대기 중")
        if not count:
            self.setToolTip("magic_converter · Shift: 변환 / Ctrl: 액션")

    def set_progress(self, progress: JobProgress) -> None:
        percent = progress.overall_percent
        status = f"{percent}%" if percent is not None else progress.phase
        count = progress.finished_files if progress.aggregate else progress.file_index
        text = f"{progress.action_label} · {status} ({count}/{progress.total_files})"
        self.status_action.setText(text)
        self.setToolTip(f"magic_converter · {text}\n{progress.filename}"[:120])

    def notify_error(self, message: str) -> None:
        self.showMessage("magic_converter · 오류", message[:900], QSystemTrayIcon.MessageIcon.Warning, 7000)

    def notify_result(self, result: JobResult) -> None:
        if result.cancelled:
            self.showMessage("magic_converter", f"{result.action.label}: 취소됨 · {result.completed}개 완료",
                             QSystemTrayIcon.MessageIcon.Information, 4000)
            return
        if result.errors:
            text = f"{result.action.label}: {result.completed}/{result.total}개 완료\n{result.errors[0]}"
            self.notify_error(text)
        else:
            text = f"{result.action.label}: {result.completed}개 완료"
            if result.outputs:
                text += f"\n{result.outputs[0]}"
                if len(result.outputs) > 1:
                    text += f"\n외 {len(result.outputs) - 1}개"
            if result.warnings:
                text += "\n" + "\n".join(dict.fromkeys(result.warnings))
            icon = QSystemTrayIcon.MessageIcon.Warning if result.warnings else QSystemTrayIcon.MessageIcon.Information
            self.showMessage("magic_converter · 완료", text[:900], icon, 7000 if result.warnings else 5000)

    def begin_shutdown(self) -> None:
        self.enabled_action.setEnabled(False)
        self.settings_action.setEnabled(False)
        if self._settings_dialog is not None:
            self._settings_dialog.reject()
        self.status_action.setText("진행 중인 작업 완료 후 종료합니다…")
        self.showMessage("magic_converter", "파일 작업이 끝나면 자동으로 종료합니다.",
                         QSystemTrayIcon.MessageIcon.Information, 4000)
