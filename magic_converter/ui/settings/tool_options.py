"""Edit only the parameters a tool actually supports, as a settings draft."""

from PySide6.QtWidgets import QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel, QLineEdit, QSpinBox, QVBoxLayout

from magic_converter.core.settings.preferences import ToolOptions
from magic_converter.core.actions.tool_definitions import TOOL_BY_KEY, TOOL_FIELD_LABELS as LABELS, tool_summary as describe_tool


class ToolOptionsDialog(QDialog):
    def __init__(self, key, options, parent=None):
        super().__init__(parent)
        self.key = key
        self.setWindowTitle(TOOL_BY_KEY[key].label + " · 옵션")
        self.setMinimumWidth(470)
        layout = QVBoxLayout(self)
        note = QLabel(TOOL_BY_KEY[key].description)
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.fields = {}
        for field in TOOL_BY_KEY[key].fields:
            value = getattr(options, field)
            if field == "pdf_pages":
                widget = QLineEdit(value)
                widget.setPlaceholderText("전체 또는 1-3,5 · 3,1처럼 순서 변경 가능")
            elif field in {"rotation", "flip"}:
                widget = QComboBox()
                for label, data in ([(f"{angle}°", angle) for angle in (0, 90, 180, 270)] if field == "rotation" else
                                    [("좌우", "horizontal"), ("상하", "vertical")]):
                    widget.addItem(label, data)
                widget.setCurrentIndex(widget.findData(value))
            else:
                widget = QSpinBox()
                if field == "subtitle_offset_ms":
                    low, high = -604800000, 604800000
                elif field in {"start_ms", "duration_ms"}:
                    low, high = 0, 604800000
                elif field == "frame_interval":
                    low, high = 1, 10000
                elif field == "max_frames":
                    low, high = 1, 100000
                else:
                    low, high = 0, 100000
                widget.setRange(low, high)
                widget.setValue(value)
                if field.endswith("_ms"):
                    widget.setSuffix(" ms")
                    widget.setSingleStep(100)
                elif field.startswith("crop_"):
                    widget.setSuffix(" px")
            self.fields[field] = widget
            form.addRow(LABELS[field], widget)
        layout.addLayout(form)
        self.error = QLabel()
        self.error.setWordWrap(True)
        self.error.setStyleSheet("color: #c33c3c")
        layout.addWidget(self.error)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QDialogButtonBox.StandardButton.Ok).setText("적용")
        buttons.button(QDialogButtonBox.StandardButton.Cancel).setText("취소")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def value(self):
        values = {}
        for field, widget in self.fields.items():
            values[field] = widget.text() if isinstance(widget, QLineEdit) else (
                widget.currentData() if isinstance(widget, QComboBox) else widget.value())
        return ToolOptions(**values)

    def accept(self):
        try:
            self.value()
        except ValueError as exc:
            self.error.setText(str(exc))
            return
        super().accept()
