"""A nonactivating, per-monitor-DPI-aware Qt OLE drop target."""

from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Property, QEasingCurve, QPoint, QPointF, QPropertyAnimation, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QDragEnterEvent, QDragLeaveEvent, QDragMoveEvent, QDropEvent, QFont, QKeyEvent, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QWidget

from magic_converter.core.conversion.file_router import Mode, Slot, describe_files, normalize_paths
from magic_converter.core.engines.office_engine import presentation_available
from magic_converter.core.conversion.conversion_request import DropSelection
from magic_converter.core.settings.preferences import Preferences, default_preferences
from magic_converter.core.settings.wheel_profiles import profile_slots


def slice_at(x: float, y: float, count: int, *, center: float = 160.0,
             inner: float = 55.0, outer: float = 149.0) -> int:
    """Clockwise indices, with slice zero centered at twelve o'clock."""
    dx, dy = x - center, y - center
    if count <= 0 or not inner <= math.hypot(dx, dy) <= outer:
        return -1
    step = math.tau / count
    angle = (math.atan2(dy, dx) + math.pi / 2 + step / 2) % math.tau
    return min(count - 1, int(angle / step))


class RadialWheelWindow(QWidget):
    action_dropped = Signal(object, str)
    request_dropped = Signal(object)
    drag_finished = Signal()
    input_rejected = Signal(str)

    def __init__(self, ffmpeg_available: Callable[[], bool], parent: QWidget | None = None, *,
                 preferences: Callable[[], Preferences] = default_preferences) -> None:
        super().__init__(parent)
        self.setWindowFlags(Qt.WindowType.FramelessWindowHint | Qt.WindowType.WindowStaysOnTopHint
                            | Qt.WindowType.Tool | Qt.WindowType.WindowDoesNotAcceptFocus)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setAcceptDrops(True)
        self.setMouseTracking(True)
        self.setFixedSize(320, 320)
        self.setWindowTitle("magic_converter")
        self._ffmpeg_available = ffmpeg_available
        self._preferences = preferences
        self._drag_preferences = preferences()
        self.mode: Mode = "convert"
        self.paths: tuple[Path, ...] = ()
        self.web_inputs = ()
        self.web_output = lambda: ""
        self._drag_web_output = ""
        self.slots: tuple[Slot, ...] = ()
        self.hovered = -1
        self._progress = 1.0
        self._wheel_scale = 0.0
        self._drag_inside = False
        self._released_inside = False
        self._native_modifiers = Qt.KeyboardModifier.NoModifier
        self._native_release_confirmed = False
        self._animation = QPropertyAnimation(self, b"hoverProgress", self)
        self._animation.setDuration(120)
        self._animation.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._open_animation = QPropertyAnimation(self, b"wheelScale", self)
        self._open_animation.setDuration(150)
        spring = QEasingCurve(QEasingCurve.Type.OutBack)
        spring.setOvershoot(0.7)
        self._open_animation.setEasingCurve(spring)
        self._open_animation.setStartValue(0.0)
        self._open_animation.setEndValue(1.0)
        self._release_timer = QTimer(self)
        self._release_timer.setSingleShot(True)
        self._release_timer.timeout.connect(self.dismiss)

    def _get_progress(self) -> float:
        return self._progress

    def _set_progress(self, value: float) -> None:
        self._progress = value
        self.update()

    hoverProgress = Property(float, _get_progress, _set_progress)

    def _get_wheel_scale(self) -> float:
        return self._wheel_scale

    def _set_wheel_scale(self, value: float) -> None:
        self._wheel_scale = max(0.0, value)
        self.update()

    wheelScale = Property(float, _get_wheel_scale, _set_wheel_scale)

    def show_for_drag(self, mode: str, cursor: QPoint) -> None:
        self._release_timer.stop()
        already_visible = self.isVisible()
        self.mode = "action" if mode == "action" else "convert"
        self._released_inside = False
        self._native_modifiers = Qt.KeyboardModifier.NoModifier
        self._native_release_confirmed = False
        if not already_visible:
            self._drag_preferences = self._preferences()
            self._drag_web_output = self.web_output()
            self.paths = ()
            self.web_inputs = ()
            self._drag_inside = False
            # Qt global logical coordinates can include negative monitor
            # origins. Never divide these coordinates by devicePixelRatio.
            # Keep the requested exact cursor center, even at a screen edge.
            self.move(cursor - QPoint(self.width() // 2, self.height() // 2))
        self._rebuild_slots()
        self.show()
        self.raise_()
        if not already_visible:
            self._open_animation.start()

    def request_dismiss(self, reason: str) -> None:
        if (reason == "modifier_released" and self._drag_inside and self.paths
                and self._drag_preferences.release_modifier_inside):
            self._released_inside = True
            self.update()
        elif reason == "mouse_released" and self._drag_inside:
            # A polling tick can observe button-up before Qt delivers the
            # native OLE Drop callback. Keep the registered HWND alive for
            # two ticks; dropEvent itself always hides it synchronously.
            self._release_timer.start(30_000 if self._drag_preferences.release_modifier_inside else 100)
        else:
            self.dismiss()

    def dismiss(self) -> None:
        self._release_timer.stop()
        self._animation.stop()
        self._open_animation.stop()
        self._set_wheel_scale(0.0)
        self.hide()
        self._drag_inside = False
        self._released_inside = False
        self._native_modifiers = Qt.KeyboardModifier.NoModifier
        self._native_release_confirmed = False
        self.paths = ()
        self.web_inputs = ()
        self.hovered = -1

    def _rebuild_slots(self) -> None:
        self.slots = self._slots(self.paths)
        self._set_hover(-1)
        self.update()

    def _slots(self, paths: tuple[Path, ...]) -> tuple[Slot, ...]:
        pass
        from magic_converter.core.automation.native_drop import has_temporary
        if self.web_inputs or (self._drag_preferences.release_modifier_inside and has_temporary(paths)):
            from magic_converter.core.actions.tool_definitions import COMBINED_ACTIONS
            slots = tuple(replace(slot, enabled=False, reason="웹 입력은 파일별 변환 작업을 선택하세요.")
                          if slot.action.key in {*COMBINED_ACTIONS, "copy_path", "terminal"} else slot for slot in slots)
        return slots

    @staticmethod
    def _local_paths(event: QDropEvent) -> tuple[Path, ...]:
        mime = event.mimeData()
        if not mime.hasUrls():
            return ()
        urls = mime.urls()
        # Reject mixed remote/local payloads rather than silently dropping
        # some of the user's selection. Filesystem I/O stays in the worker.
        if not urls or any(not url.isLocalFile() or not url.toLocalFile() for url in urls):
            return ()
        return normalize_paths(url.toLocalFile() for url in urls)

    @staticmethod
    def _accept_copy(event: QDropEvent) -> bool:
        if not event.possibleActions() & Qt.DropAction.CopyAction:
            event.ignore()
            return False
        if event.proposedAction() == Qt.DropAction.CopyAction:
            event.acceptProposedAction()
        else:
            # Explorer proposes MoveAction for Shift. Returning MoveAction
            # can cause a source to delete its originals after a drop.
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        return True

    def _set_hover(self, index: int) -> None:
        if index == self.hovered:
            return
        self.hovered = index
        selected = self.slots[index] if 0 <= index < len(self.slots) else None
        self.setToolTip((selected.action.description if selected.enabled else selected.reason) if selected else "")
        self._animation.stop()
        self._animation.setStartValue(0.0)
        self._animation.setEndValue(1.0)
        self._animation.start()
        self.update()

    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        from magic_converter.core.automation.web_inputs import from_mime
        paths, web = from_mime(event.mimeData())
        if not paths or not self._accept_copy(event):
            event.ignore()
            return
        self._drag_inside = True
        self._native_modifiers = event.modifiers()
        self.web_inputs = web
        self.paths = paths
        self._rebuild_slots()
        position = event.position()
        self._set_hover(slice_at(position.x(), position.y(), len(self.slots)))

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        self._native_modifiers = event.modifiers()
        if self._drag_preferences.release_modifier_inside:
            modified = self._native_modifiers & (Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)
            if modified:
                self._native_release_confirmed = False
            elif event.buttons() & Qt.MouseButton.LeftButton:
                # Confirm and answer in the SAME native key-change callback.
                # Windows Qt caches effects for an identical point/key state
                # even with an empty answer rect. Waiting for our polling timer
                # would leave a refusal cached after showing a ready wheel.
                self._released_inside = True
                self._native_release_confirmed = True
        if not self.paths:
            event.ignore()
            return
        position = event.position()
        index = slice_at(position.x(), position.y(), len(self.slots))
        self._set_hover(index)
        if index >= 0 and self.slots[index].enabled and self._ready_for_drop():
            self._accept_copy(event)
        else:
            event.ignore()
        self.update()

    def _ready_for_drop(self) -> bool:
        if not self._drag_preferences.release_modifier_inside:
            return True
        # A source can retain Shift's Move semantics even when this target
        # returns Copy. Require key-up while the mouse is still held, plus an
        # unmodified OLE event. Button-up followed by key-up must never arm it.
        return self._native_release_confirmed and not self._native_modifiers & (
            Qt.KeyboardModifier.ShiftModifier | Qt.KeyboardModifier.ControlModifier)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        self._drag_inside = False
        self._set_hover(-1)
        event.accept()
        if self._released_inside:
            self.dismiss()
            self.drag_finished.emit()

    def dropEvent(self, event: QDropEvent) -> None:
        self._native_modifiers = event.modifiers()
        if not self._ready_for_drop():
            event.ignore()
            self.dismiss()
            self.drag_finished.emit()
            return
        from magic_converter.core.automation.web_inputs import from_mime
        paths, web = from_mime(event.mimeData())
        position = event.position()
        # Use the displayed geometry and frozen recipe, never a newly loaded
        # profile whose reordering could turn this position into another action.
        slots = self.slots
        index = slice_at(position.x(), position.y(), len(slots))
        if paths and paths == self.paths and web == self.web_inputs and index >= 0 and slots[index].enabled and self._accept_copy(event):
            key = slots[index].action.key
            if self._drag_preferences.release_modifier_inside and not web:
                from magic_converter.core.automation.native_drop import snapshot_temporary
                try:
                    paths, web = snapshot_temporary(paths)
                except (OSError, ValueError) as error:
                    event.ignore()
                    self.dismiss()
                    self.drag_finished.emit()
                    self.input_rejected.emit(f"임시 드래그 입력을 보존하지 못했습니다: {error}")
                    return
            output = self._drag_preferences.output
            if web and output.location == "source" and self._drag_web_output:
                output = replace(output, location="folder", directory=self._drag_web_output)
            selection = DropSelection(paths, key, slots[index].preset, output, web)
            self.dismiss()
            self.drag_finished.emit()
            self.request_dropped.emit(selection)
            self.action_dropped.emit(paths, key)
        else:
            event.ignore()
            self.dismiss()
            self.drag_finished.emit()

    def keyPressEvent(self, event: QKeyEvent) -> None:
        if event.key() == Qt.Key.Key_Escape:
            self.dismiss()
            self.drag_finished.emit()
        else:
            super().keyPressEvent(event)

    def paintEvent(self, event: object) -> None:
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(Qt.PenStyle.NoPen)
        # Alpha=1 prevents Windows' layered-window transparent hit testing
        # from passing OLE input through the center or the gaps in the ring.
        painter.setBrush(QColor(0, 0, 0, 1))
        painter.drawEllipse(QRectF(7, 7, 306, 306))
        # Animate the painted radius, not the native OLE target's geometry.
        # Alpha=1 coverage and drop hit areas remain stable during opening.
        painter.translate(160, 160)
        painter.scale(self._wheel_scale, self._wheel_scale)
        painter.translate(-160, -160)
        count = len(self.slots)
        if count:
            step = 360.0 / count
            outer = QRectF(13, 13, 294, 294)
            inner = QRectF(105, 105, 110, 110)
            for index, slot in enumerate(self.slots):
                start = 90 + step / 2 - index * step - 1.0
                span = step - 2.0
                radians = math.radians(start)
                path = QPainterPath()
                path.moveTo(160 + 147 * math.cos(radians), 160 - 147 * math.sin(radians))
                path.arcTo(outer, start, -span)
                end = math.radians(start - span)
                path.lineTo(160 + 55 * math.cos(end), 160 - 55 * math.sin(end))
                path.arcTo(inner, start - span, span)
                path.closeSubpath()
                hovered = index == self.hovered
                enabled = slot.enabled and self._ready_for_drop()
                if hovered and enabled:
                    color = QColor(255, 140, 0, int(190 + 50 * self._progress))
                elif hovered:
                    color = QColor(68, 61, 59, 235)
                else:
                    color = QColor(30, 30, 35, 225 if enabled else 180)
                painter.setBrush(color)
                painter.setPen(QPen(QColor(255, 255, 255, 24), 1))
                painter.drawPath(path)
                angle = math.radians(index * step - 90)
                x, y = 160 + 101 * math.cos(angle), 160 + 101 * math.sin(angle)
                painter.setFont(QFont("Segoe UI", 9 if count > 7 else 10, QFont.Weight.DemiBold))
                painter.setPen(QColor("#201b13") if hovered and enabled else
                               QColor("#f0f0f3") if enabled else QColor("#85858e"))
                label_width = 64 if count > 7 else 86
                label = slot.action.label.replace(" (", "\n(") if slot.action.key in {"pptx_image", "pptx_editable"} else slot.action.label
                if slot.action.key == "strip":
                    label = label.replace(" ", "\n", 1)
                if slot.action.key.startswith("tool:"):
                    from magic_converter.core.actions.tool_definitions import TOOL_BY_KEY
                    tool = TOOL_BY_KEY[slot.action.key]
                    if slot.action.label == tool.label:
                        label = tool.wheel_label
                label = "\n".join(painter.fontMetrics().elidedText(line, Qt.TextElideMode.ElideRight, label_width)
                                  for line in label.split("\n")[:2])
                painter.drawText(QRectF(x - label_width / 2, y - 20, label_width, 40), Qt.AlignmentFlag.AlignCenter, label)

        painter.setPen(QPen(QColor(255, 255, 255, 28), 1))
        painter.setBrush(QColor(23, 23, 28, 245))
        painter.drawEllipse(QRectF(110, 110, 100, 100))
        painter.setPen(QColor("#ff9c32"))
        painter.setFont(QFont("Segoe UI", 11, QFont.Weight.DemiBold))
        painter.drawText(QRectF(112, 128, 96, 23), Qt.AlignmentFlag.AlignCenter,
                         "Convert" if self.mode == "convert" else "Action")
        painter.setFont(QFont("Segoe UI", 8))
        painter.setPen(QColor("#c7c7cf"))
        metrics = painter.fontMetrics()
        caption = metrics.elidedText(describe_files(self.paths), Qt.TextElideMode.ElideRight, 94)
        painter.drawText(QRectF(112, 154, 96, 19), Qt.AlignmentFlag.AlignCenter, caption)
        painter.setFont(QFont("Segoe UI", 8))
        selected = self.slots[self.hovered] if 0 <= self.hovered < len(self.slots) else None
        hint = selected.action.label if selected and selected.enabled else selected.reason if selected else "드롭하여 실행"
        if self._drag_preferences.release_modifier_inside and self._drag_inside:
            hint = "이제 놓으세요" if self._ready_for_drop() else "키를 먼저 놓기"
        if selected and not selected.enabled:
            hint = "Office 필요" if "PowerPoint" in selected.reason else hint
        hint = painter.fontMetrics().elidedText(hint, Qt.TextElideMode.ElideRight, 94)
        painter.setPen(QColor("#ffb65d") if selected and selected.enabled else QColor("#92929c"))
        painter.drawText(QRectF(112, 175, 96, 17), Qt.AlignmentFlag.AlignCenter, hint)
        painter.end()
