"""User-facing support matrix including fidelity and engine requirements."""

from PySide6.QtWidgets import QDialog, QDialogButtonBox, QTextBrowser, QVBoxLayout, QWidget


class FormatHelpDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("magic_converter · 지원 형식")
        self.resize(760, 620)
        layout = QVBoxLayout(self)
        text = QTextBrowser()
        pass
        layout.addWidget(text)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
