"""Draft editors for recipes, ordered wheel slots, output policy and history."""

from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QFileDialog, QFormLayout,
    QFrame, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem, QPushButton,
    QScrollArea, QSpinBox, QToolButton, QVBoxLayout, QWidget)

from magic_converter.core.conversion.file_router import ALL_ACTIONS
from magic_converter.core.settings.preferences import OutputRule, Preferences, ToolOptions
from magic_converter.core.actions.tool_definitions import TOOL_BY_KEY


def add_settings_tab(tabs, content, label):
    scroll = QScrollArea()
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    scroll.setWidgetResizable(True)
    scroll.setWidget(content)
    tabs.addTab(scroll, label)
    return scroll


def spin(low, high, value, suffix=""):
    widget = QSpinBox()
    widget.setRange(low, high)
    widget.setValue(value)
    widget.setSuffix(suffix)
    return widget


class OutputEditor(QWidget):
    def __init__(self):
        super().__init__()
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.location = QComboBox()
        self.location.addItem("원본 폴더", "source")
        self.location.addItem("지정 폴더", "folder")
        form.addRow("저장 위치", self.location)
        row = QHBoxLayout()
        self.directory = QLineEdit()
        self.browse = QPushButton("찾기…")
        self.browse.clicked.connect(self._browse)
        row.addWidget(self.directory)
        row.addWidget(self.browse)
        form.addRow("출력 폴더", row)
        self.template = QLineEdit()
        self.template.setPlaceholderText("{stem}_{tag}")
        self.template.setToolTip("{stem}: 원본 이름 · {tag}: converted/slide/editable · {preset}: 프리셋 이름\n확장자는 자동으로 붙습니다.")
        form.addRow("파일/폴더 이름", self.template)
        self.collision = QComboBox()
        self.collision.addItem("번호 추가 · 기존 파일 보존", "rename")
        self.collision.addItem("실패로 기록 · 기존 파일 보존", "error")
        form.addRow("이름 충돌", self.collision)
        self.location.currentIndexChanged.connect(self._update)
        self.set_rule(OutputRule())

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "출력 폴더", self.directory.text())
        if path:
            self.directory.setText(path)

    def _update(self):
        enabled = self.location.currentData() == "folder"
        self.directory.setEnabled(enabled)
        self.browse.setEnabled(enabled)

    def set_rule(self, rule: OutputRule):
        from dataclasses import asdict
        self.set_raw(asdict(rule))

    def set_raw(self, raw):
        self.location.setCurrentIndex(self.location.findData(raw["location"]))
        self.directory.setText(raw["directory"])
        self.template.setText(raw["template"])
        self.collision.setCurrentIndex(self.collision.findData(raw["collision"]))
        self._update()

    def raw_value(self):
        return {"location": self.location.currentData(), "directory": self.directory.text().strip(),
                "template": self.template.text(), "collision": self.collision.currentData()}


class PreferencesEditor:
    """Owns three tab pages. All changes remain local until SettingsDialog saves."""
    def __init__(self, tabs, preferences: Preferences):
        self._preset_row = self._profile_row = -1
        self._loading_profile = False
        self._data = preferences.to_dict()
        self.presets_page = QWidget()
        layout = QHBoxLayout(self.presets_page)
        left = QVBoxLayout()
        self.preset_list = QListWidget()
        self.preset_list.setMaximumWidth(220)
        left.addWidget(self.preset_list)
        buttons = QHBoxLayout()
        for label, callback in (("추가", self._add_preset), ("복제", self._copy_preset), ("삭제", self._delete_preset)):
            button = QToolButton()
            button.setText(label)
            button.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
            button.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        left.addLayout(buttons)
        layout.addLayout(left)
        self.preset_form = QWidget()
        form = QFormLayout(self.preset_form)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.WrapLongRows)
        self.preset_name = QLineEdit()
        self.action = QComboBox()
        for action in ALL_ACTIONS.values():
            family = {"image": "이미지", "document": "문서·PDF", "sheet": "표", "subtitle": "자막",
                      "archive": "압축", "media": "영상·미디어", "audio": "오디오", "tool": "도구"}.get(
                          action.key.split(":")[0], "작업")
            self.action.addItem(f"{family} · {action.label}", action.key)
        form.addRow("프리셋 이름", self.preset_name)
        form.addRow("변환 / 작업", self.action)
        self.quality = spin(1, 100, 90)
        self.edge = spin(0, 16384, 0, " px")
        self.edge.setSpecialValueText("원본 크기")
        form.addRow("이미지 품질", self.quality)
        form.addRow("이미지 긴 변", self.edge)
        self.crf = spin(-1, 51, -1)
        self.crf.setSpecialValueText("기본값")
        form.addRow("영상 CRF", self.crf)
        self.bitrate = QComboBox()
        self.bitrate.setEditable(True)
        self.bitrate.addItem("기본값", None)
        for value in (64, 96, 128, 192, 256, 320, 512):
            self.bitrate.addItem(str(value), value)
        self.bitrate.setToolTip("32~512 kbps · 빈 값/기본값은 기존 인코더 설정 사용")
        form.addRow("오디오 kbps", self.bitrate)
        self._tool_options = ToolOptions()
        self._tool_action = ""
        self.tool_button = QPushButton("도구 옵션…")
        self.tool_button.clicked.connect(self._edit_tool)
        self.tool_summary = QLabel()
        self.tool_summary.setWordWrap(True)
        form.addRow(self.tool_button)
        form.addRow(self.tool_summary)
        self.preset_output_group = QGroupBox("프리셋별 출력 설정 (해제 시 공통 설정 사용)")
        self.preset_output_group.setCheckable(True)
        group_layout = QVBoxLayout(self.preset_output_group)
        self.preset_output = OutputEditor()
        group_layout.addWidget(self.preset_output)
        form.addRow(self.preset_output_group)
        note = QLabel("이미지는 비율을 유지하고 확대하지 않습니다. CRF는 낮을수록 고품질입니다.\n"
                      "해당 작업에서 지원하는 옵션만 활성화됩니다.")
        note.setWordWrap(True)
        form.addRow(note)
        layout.addWidget(self.preset_form, 1)
        add_settings_tab(tabs, self.presets_page, "프리셋")

        self.profiles_page = QWidget()
        layout = QVBoxLayout(self.profiles_page)
        active = QFormLayout()
        self.convert_profile, self.action_profile = QComboBox(), QComboBox()
        active.addRow("Shift 변환 휠", self.convert_profile)
        active.addRow("Ctrl 액션 휠", self.action_profile)
        layout.addLayout(active)
        columns = QHBoxLayout()
        left = QVBoxLayout()
        self.profile_list = QListWidget()
        self.profile_list.setMaximumWidth(220)
        left.addWidget(self.profile_list)
        buttons = QHBoxLayout()
        for label, callback in (("추가", self._add_profile), ("삭제", self._delete_profile)):
            button = QPushButton(label)
            button.clicked.connect(callback)
            buttons.addWidget(button)
        left.addLayout(buttons)
        columns.addLayout(left)
        self.profile_form = QWidget()
        form = QFormLayout(self.profile_form)
        self.profile_name = QLineEdit()
        self.profile_mode = QComboBox()
        self.profile_mode.addItem("Shift · 변환", "convert")
        self.profile_mode.addItem("Ctrl · 액션", "action")
        form.addRow("프로필 이름", self.profile_name)
        form.addRow("종류", self.profile_mode)
        self.members = QListWidget()
        self.members.setDragDropMode(QAbstractItemView.DragDropMode.InternalMove)
        self.members.setDefaultDropAction(Qt.DropAction.MoveAction)
        form.addRow("슬롯 (1~12개)", self.members)
        note = QLabel("체크한 프리셋만 표시합니다. 드래그로 순서를 바꿀 수 있습니다.\n"
                      "첫 슬롯은 12시 방향입니다. 지원하지 않는 파일은 슬롯이 비활성화됩니다.")
        note.setWordWrap(True)
        form.addRow(note)
        columns.addWidget(self.profile_form, 1)
        layout.addLayout(columns, 1)
        profile_tab = add_settings_tab(tabs, self.profiles_page, "휠 프로필")

        self.output_page = QWidget()
        layout = QVBoxLayout(self.output_page)
        layout.addWidget(QLabel("프리셋에 별도 출력 설정이 없으면 아래 설정을 사용합니다."))
        self.output = OutputEditor()
        layout.addWidget(self.output)
        note = QLabel("사용 변수: {stem} 원본 이름 · {tag} 작업 종류 · {preset} 프리셋 이름\n"
                      "확장자는 자동 지정됩니다. 페이지 이미지·여러 시트는 이 이름의 폴더로 저장됩니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.history_limit = spin(0, 1000, 200, "개 작업")
        self.history_limit.setSpecialValueText("새 기록 저장 안 함")
        form.addRow("결과 기록 보관", self.history_limit)
        layout.addLayout(form)
        note = QLabel("기록은 이 PC에 경로·성공/실패·경고를 저장합니다. 파일 내용은 저장하지 않습니다.\n"
                      "0으로 설정하면 새 기록을 저장하지 않습니다. 기존 기록은 결과 기록 창에서 지울 수 있습니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch()
        add_settings_tab(tabs, self.output_page, "출력·기록")

        self.preset_list.currentRowChanged.connect(self._select_preset)
        self.profile_list.currentRowChanged.connect(self._select_profile)
        self.action.currentIndexChanged.connect(self._options_enabled)
        self.preset_name.textChanged.connect(self._preset_label)
        self.profile_name.textChanged.connect(self._profile_label)
        self.profile_mode.currentIndexChanged.connect(self._profile_identity_changed)
        tabs.currentChanged.connect(lambda index: self._select_profile(self._profile_row)
                                    if tabs.widget(index) is profile_tab else None)
        self.set_preferences(preferences)

    def _preset_label(self, text):
        if self._preset_row >= 0:
            self.preset_list.item(self._preset_row).setText(text or "(이름 없음)")

    def _profile_label(self, text):
        if self._profile_row >= 0:
            self.profile_list.item(self._profile_row).setText(text or "(이름 없음)")
            self._profile_identity_changed()

    def _profile_identity_changed(self):
        if self._profile_row >= 0 and not self._loading_profile:
            self._flush_profile()
            self._refresh_profile_selectors()

    def _options_enabled(self):
        key = self.action.currentData() or ""
        if self._tool_action != key:
            self._tool_options = ToolOptions()
            self._tool_action = key
        self._tool_description()
        for widget in (self.quality, self.edge):
            widget.setEnabled(key.startswith("image:"))
        self.crf.setEnabled(key in {"media:mp4", "media:mov", "media:mkv", "media:webm"})
        self.bitrate.setEnabled(key in {"media:mp4", "media:mov", "media:mkv", "media:webm", "media:avi", "media:wmv",
                                        "media:mp3", "audio:m4a", "audio:ogg", "audio:opus", "audio:wma"})

    def _tool_description(self):
        from magic_converter.ui.settings.tool_options import describe_tool
        key = self.action.currentData()
        self.tool_button.setVisible(key in TOOL_BY_KEY)
        self.tool_button.setEnabled(key in TOOL_BY_KEY and bool(TOOL_BY_KEY[key].fields))
        self.tool_summary.setVisible(key in TOOL_BY_KEY)
        self.tool_summary.setText(describe_tool(key, self._tool_options) if key in TOOL_BY_KEY else "")

    def _edit_tool(self):
        from magic_converter.ui.settings.tool_options import ToolOptionsDialog
        key = self.action.currentData()
        if key in TOOL_BY_KEY:
            dialog = ToolOptionsDialog(key, self._tool_options, self.presets_page)
            if dialog.exec() == dialog.DialogCode.Accepted:
                self._tool_options = dialog.value()
                self._tool_description()
            dialog.deleteLater()

    def _flush_preset(self):
        if self._preset_row < 0:
            return
        raw = self._data["presets"][self._preset_row]
        bitrate = self.bitrate.currentText().strip()
        # Keep invalid text in the draft; schema validation on Save reports it.
        bitrate = None if not bitrate or bitrate == "기본값" else int(bitrate) if bitrate.isdecimal() else bitrate
        raw.update(name=self.preset_name.text().strip(), action_key=self.action.currentData(),
                   options={"image_quality": self.quality.value() if self.quality.isEnabled() else 90,
                            "image_max_edge": self.edge.value() if self.edge.isEnabled() else 0,
                            "video_crf": self.crf.value() if self.crf.isEnabled() and self.crf.value() >= 0 else None,
                            "audio_bitrate": bitrate if self.bitrate.isEnabled() else None},
                   output=self.preset_output.raw_value() if self.preset_output_group.isChecked() else None)
        from dataclasses import asdict
        raw["tools"] = asdict(self._tool_options.for_action(self.action.currentData()))

    def _select_preset(self, row):
        self._flush_preset()
        self._preset_row = row
        self.preset_form.setEnabled(row >= 0)
        if row < 0:
            return
        raw = self._data["presets"][row]
        self.preset_name.setText(raw["name"])
        self.action.setCurrentIndex(self.action.findData(raw["action_key"]))
        options = raw["options"]
        self.quality.setValue(options["image_quality"])
        self.edge.setValue(options["image_max_edge"])
        self.crf.setValue(options["video_crf"] if options["video_crf"] is not None else -1)
        self.bitrate.setCurrentText(str(options["audio_bitrate"]) if options["audio_bitrate"] is not None else "기본값")
        self._tool_action = raw["action_key"]
        self._tool_options = ToolOptions(**raw.get("tools", {}))
        self.preset_output_group.setChecked(raw["output"] is not None)
        if raw["output"]:
            self.preset_output.set_raw(raw["output"])
        else:
            self.preset_output.set_rule(OutputRule())
        self._options_enabled()

    def _add_preset(self):
        self._flush_preset()
        from dataclasses import asdict
        from magic_converter.core.settings.preferences import Preset
        self._data["presets"].append(asdict(Preset(uuid4().hex, "새 프리셋", "image:jpg")))
        self.preset_list.addItem("새 프리셋")
        self.preset_list.setCurrentRow(len(self._data["presets"]) - 1)

    def _copy_preset(self):
        if self._preset_row < 0:
            return
        self._flush_preset()
        raw = deepcopy(self._data["presets"][self._preset_row])
        raw.update(id=uuid4().hex, name=raw["name"][:55] + " 복사")
        self._data["presets"].append(raw)
        self.preset_list.addItem(raw["name"])
        self.preset_list.setCurrentRow(len(self._data["presets"]) - 1)

    def _delete_preset(self):
        if self._preset_row < 0:
            return
        self._flush_profile()
        removed = self._data["presets"].pop(self._preset_row)["id"]
        for profile in self._data["profiles"]:
            profile["preset_ids"] = [id for id in profile["preset_ids"] if id != removed]
        self._data["profiles"] = [profile for profile in self._data["profiles"] if profile["preset_ids"]]
        self._reload_lists()

    def _flush_profile(self):
        if self._profile_row >= 0:
            self._data["profiles"][self._profile_row].update(
                name=self.profile_name.text().strip(), mode=self.profile_mode.currentData(),
                preset_ids=[self.members.item(i).data(Qt.ItemDataRole.UserRole) for i in range(self.members.count())
                            if self.members.item(i).checkState() == Qt.CheckState.Checked])

    def _select_profile(self, row):
        self._flush_profile()
        self._flush_preset()
        self._profile_row = row
        self.profile_form.setEnabled(row >= 0)
        self.members.clear()
        if row < 0:
            return
        raw = self._data["profiles"][row]
        self._loading_profile = True
        self.profile_name.setText(raw["name"])
        self.profile_mode.setCurrentIndex(self.profile_mode.findData(raw["mode"]))
        presets = {preset["id"]: preset for preset in self._data["presets"]}
        for id in [*raw["preset_ids"], *(id for id in presets if id not in raw["preset_ids"])]:
            item = QListWidgetItem(presets[id]["name"], self.members)
            item.setData(Qt.ItemDataRole.UserRole, id)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            item.setCheckState(Qt.CheckState.Checked if id in raw["preset_ids"] else Qt.CheckState.Unchecked)
        self._loading_profile = False
        self._refresh_profile_selectors()

    def _refresh_profile_selectors(self):
        for mode in ("convert", "action"):
            combo = getattr(self, mode + "_profile")
            selected = combo.currentData()
            combo.clear()
            combo.addItem("자동 · 파일 종류별 기본 휠", "")
            for profile in self._data["profiles"]:
                if profile["mode"] == mode:
                    combo.addItem(profile["name"], profile["id"])
            combo.setCurrentIndex(max(0, combo.findData(selected)))

    def _add_profile(self):
        self._flush_profile()
        self._flush_preset()
        if not self._data["presets"]:
            self._add_preset()
        raw = {"id": uuid4().hex, "name": "새 프로필", "mode": "convert",
               "preset_ids": [self._data["presets"][0]["id"]]}
        self._data["profiles"].append(raw)
        self.profile_list.addItem(raw["name"])
        self.profile_list.setCurrentRow(len(self._data["profiles"]) - 1)

    def _delete_profile(self):
        if self._profile_row >= 0:
            self._flush_preset()
            self._data["profiles"].pop(self._profile_row)
            self._reload_lists()

    def _reload_lists(self):
        self._preset_row = self._profile_row = -1
        for widget, key in ((self.preset_list, "presets"), (self.profile_list, "profiles")):
            widget.blockSignals(True)
            widget.clear()
            widget.addItems([raw["name"] for raw in self._data[key]])
            widget.blockSignals(False)
        self._select_preset(-1)
        self._select_profile(-1)
        self.preset_list.setCurrentRow(0)
        self.profile_list.setCurrentRow(0)
        self._refresh_profile_selectors()

    def set_preferences(self, preferences: Preferences):
        self._data = deepcopy(preferences.to_dict())
        # dataclasses.asdict preserves tuples; drafts need mutable lists.
        self._data["presets"] = list(self._data["presets"])
        self._data["profiles"] = list(self._data["profiles"])
        self._reload_lists()
        for mode in ("convert", "action"):
            combo = getattr(self, mode + "_profile")
            combo.setCurrentIndex(max(0, combo.findData(getattr(preferences, mode + "_profile"))))
        self.output.set_rule(preferences.output)
        self.history_limit.setValue(preferences.history_limit)

    def value(self) -> Preferences:
        self._flush_preset()
        self._flush_profile()
        self._refresh_profile_selectors()
        self._data.update(convert_profile=self.convert_profile.currentData(), action_profile=self.action_profile.currentData(),
                          output=self.output.raw_value(), history_limit=self.history_limit.value())
        return Preferences.from_dict(self._data)
