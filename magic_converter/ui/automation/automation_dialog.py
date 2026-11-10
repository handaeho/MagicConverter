"""Explicit watch configuration, retry/stop controls and Explorer integration."""
from dataclasses import replace
from pathlib import Path
from uuid import uuid4

from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QListWidget, QPushButton, QVBoxLayout, QMessageBox)

from magic_converter.core.automation.automation_store import WatchRule


class AutomationDialog(QDialog):
    def __init__(self, controller, preferences, parent=None):
        super().__init__(parent)
        self.controller, self.preferences = controller, preferences
        self.rules = list(controller.store.rules)
        self.setWindowTitle("감시 폴더 · 웹 드래그 · 탐색기")
        self.resize(680, 590)
        layout = QVBoxLayout(self)
        self.list = QListWidget()
        self.list.currentRowChanged.connect(self.load_rule)
        layout.addWidget(self.list)
        row = QHBoxLayout()
        for text, callback in (("추가", self.add_rule), ("규칙에 반영", self.apply_rule), ("삭제", self.remove_rule)):
            button = QPushButton(text)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        form = QFormLayout()
        self.folder, self.output, self.pattern, self.web_output = QLineEdit(), QLineEdit(), QLineEdit("*"), QLineEdit(controller.store.web_output)
        self.folder.setPlaceholderText("감시할 폴더의 절대 경로")
        self.output.setPlaceholderText("별도 출력 폴더 (기본: 감시 폴더/_converted)")
        self.web_output.setPlaceholderText("비워두면 다운로드/MagicConverter")
        self.preset = QComboBox()
        for recipe in preferences.presets:
            self.preset.addItem(recipe.name, recipe)
        self.recursive, self.enabled = QCheckBox("하위 폴더 포함"), QCheckBox("규칙 사용")
        self.enabled.setChecked(True)
        self.settle = QDoubleSpinBox()
        self.settle.setRange(.1, 300)
        self.settle.setValue(2)
        self.settle.setSuffix(" 초")
        for label, field in (("감시 폴더", self.folder), ("출력 폴더", self.output), ("변환 프리셋", self.preset),
                             ("포함 패턴", self.pattern), ("안정화 대기", self.settle), ("", self.recursive),
                             ("", self.enabled), ("웹 드래그 출력", self.web_output)):
            form.addRow(label, field)
        layout.addLayout(form)
        row = QHBoxLayout()
        for text, callback in (("저장·감시 시작", self.save_start), ("감시 중지·취소", controller.stop),
                               ("실패·취소 재시도", self.retry), ("탐색기 메뉴 추가", self.install_shell),
                               ("탐색기 메뉴 제거", self.remove_shell)):
            button = QPushButton(text)
            button.clicked.connect(lambda checked=False, fn=callback: self.safely(fn))
            row.addWidget(button)
        layout.addLayout(row)
        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        layout.addWidget(QLabel("규칙의 프리셋은 저장 시 고정됩니다. 기존 파일도 검사하며, 실패·취소 항목은 재시도 전까지 보류합니다."))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        controller.changed.connect(self.refresh_status)
        self.destroyed.connect(lambda: None)
        self.refresh()

    def safely(self, function):
        try:
            function()
            self.refresh_status()
        except (OSError, ValueError) as exc:
            self.status.setText(str(exc))

    def refresh(self):
        selected = self.list.currentRow()
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems([("✓ " if rule.enabled else "○ ") + rule.folder + " → " + rule.recipe.name for rule in self.rules])
        self.list.blockSignals(False)
        self.list.setCurrentRow(min(max(0, selected), len(self.rules) - 1))
        self.refresh_status()

    def load_rule(self, index):
        if not 0 <= index < len(self.rules):
            return
        rule = self.rules[index]
        self.folder.setText(rule.folder)
        self.output.setText(rule.output)
        self.pattern.setText(rule.include)
        self.settle.setValue(rule.settle)
        self.recursive.setChecked(rule.recursive)
        self.enabled.setChecked(rule.enabled)
        index = next((i for i in range(self.preset.count()) if self.preset.itemData(i).id == rule.recipe.id), -1)
        if index < 0:
            self.preset.addItem(rule.recipe.name + " (저장된 규칙)", rule.recipe)
            index = self.preset.count() - 1
        self.preset.setCurrentIndex(index)

    def add_rule(self):
        folder = QFileDialog.getExistingDirectory(self, "감시 폴더 선택")
        if folder:
            self.folder.setText(folder)
            self.output.setText(str(Path(folder) / "_converted"))
            self.list.setCurrentRow(-1)

    def apply_rule(self):
        def apply():
            index = self.list.currentRow()
            identifier = self.rules[index].id if 0 <= index < len(self.rules) else uuid4().hex
            if not self.folder.text().strip() or self.preset.currentData() is None:
                raise ValueError("감시 폴더와 프리셋을 선택하세요.")
            recipe = self.preset.currentData()
            recipe = replace(recipe, output=recipe.output or self.preferences.output)
            rule = WatchRule(identifier, str(Path(self.folder.text()).resolve()),
                             str(Path(self.output.text()).resolve()) if self.output.text() else str(Path(self.folder.text()).resolve() / "_converted"),
                             recipe, self.recursive.isChecked(), self.pattern.text(), self.enabled.isChecked(), self.settle.value())
            if index < 0:
                self.rules.append(rule)
            else:
                self.rules[index] = rule
            self.refresh()
        self.safely(apply)

    def remove_rule(self):
        index = self.list.currentRow()
        if 0 <= index < len(self.rules):
            self.rules.pop(index)
            self.refresh()

    def save_start(self):
        if self.controller.active_count:
            raise ValueError("진행 중인 감시 작업을 중지하고 종료된 뒤 규칙을 저장하세요.")
        self.controller.stop(cancel=False)
        self.controller.store.save(self.rules, self.web_output.text().strip())
        self.controller.start()

    def retry(self):
        self.controller.retry_failed()

    def refresh_status(self):
        rows = self.controller.engine.journal.rows()
        failed = sum(row["state"] in {"failed", "cancelled", "interrupted"} for row in rows)
        detail = next((row["detail"] for row in rows if row["state"] == "failed" and row["detail"]), "")
        self.status.setText(("감시 중" if self.controller.running else "감시 중지") + f" · 진행 {self.controller.active_count} · 실패/취소 {failed}" + ("\n" + detail[:200] if detail else ""))

    def install_shell(self):
        from magic_converter.core.automation.explorer_integration import install
        install()
        self.status.setText("현재 사용자 탐색기 메뉴에 추가했습니다. Windows 11에서는 ‘더 많은 옵션 표시’에 나타납니다.")

    def remove_shell(self):
        from magic_converter.core.automation.explorer_integration import remove
        remove()
        self.status.setText("현재 사용자 탐색기 메뉴를 제거했습니다.")
