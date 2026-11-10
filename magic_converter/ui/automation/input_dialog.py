"""Review Explorer/IPC selections before submitting a frozen conversion recipe."""
from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFileDialog, QFormLayout,
                               QLabel, QLineEdit, QListWidget, QPushButton, QVBoxLayout, QHBoxLayout)

from magic_converter.core.conversion.builtin_converters import runtime_catalog
from magic_converter.core.conversion.conversion_request import DropSelection
from magic_converter.core.conversion.file_router import ALL_ACTIONS, normalize_paths
from magic_converter.core.settings.preferences import OutputRule


class InputDialog(QDialog):
    submitted = Signal(object)

    def __init__(self, paths, preferences, ffmpeg, parent=None):
        super().__init__(parent)
        self.setWindowTitle("파일 변환")
        self.resize(560, 420)
        self.preferences, self.ffmpeg = preferences, ffmpeg
        self.paths = ()
        layout = QVBoxLayout(self)
        self.files = QListWidget()
        layout.addWidget(self.files)
        form = QFormLayout()
        self.actions = QComboBox()
        form.addRow("작업 / 프리셋", self.actions)
        row = QHBoxLayout()
        self.output = QLineEdit(preferences.output.directory if preferences.output.location == "folder" else "")
        self.output.setPlaceholderText("비워두면 프리셋 설정 또는 원본 폴더")
        row.addWidget(self.output)
        browse = QPushButton("선택…")
        browse.clicked.connect(self.browse)
        row.addWidget(browse)
        form.addRow("출력 폴더", row)
        layout.addLayout(form)
        self.message = QLabel()
        self.message.setWordWrap(True)
        layout.addWidget(self.message)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("변환 시작")
        buttons.accepted.connect(self.submit)
        buttons.rejected.connect(self.reject)
        self.run_button = buttons.button(QDialogButtonBox.StandardButton.Ok)
        layout.addWidget(buttons)
        self.add_paths(paths)

    def add_paths(self, paths):
        selected = self.actions.currentData()
        self.paths = normalize_paths((*self.paths, *paths))
        self.files.clear()
        self.files.addItems([str(path) for path in self.paths])
        self.actions.clear()
        catalog = runtime_catalog(self.ffmpeg)
        for recipe in self.preferences.presets:
            if recipe.action_key not in {"copy_path", "terminal"} and all(catalog.assess(path, recipe.action_key).route for path in self.paths):
                self.actions.addItem("프리셋 · " + recipe.name, (recipe.action_key, recipe.id))
        for key, action in ALL_ACTIONS.items():
            if key not in {"copy_path", "terminal"} and all(catalog.assess(path, key).route for path in self.paths):
                self.actions.addItem(action.label, (key, ""))
        index = self.actions.findData(selected)
        if index >= 0:
            self.actions.setCurrentIndex(index)
        self.run_button.setEnabled(bool(self.actions.count() and self.paths))
        self.message.setText(f"{len(self.paths)}개 파일 · 모든 파일에 적용할 수 있는 작업을 표시합니다.")

    def browse(self):
        folder = QFileDialog.getExistingDirectory(self, "출력 폴더 선택", self.output.text())
        if folder:
            self.output.setText(folder)

    def submit(self):
        if not self.actions.currentData():
            return
        key, preset_id = self.actions.currentData()
        recipe = next((p for p in self.preferences.presets if p.id == preset_id), None)
        output = self.preferences.output
        try:
            if self.output.text().strip():
                output = replace(output, location="folder", directory=str(Path(self.output.text()).resolve()))
                if recipe:
                    recipe = replace(recipe, output=output)
            self.submitted.emit(DropSelection(self.paths, key, recipe, output))
            self.accept()
        except ValueError as exc:
            self.message.setText(str(exc))
