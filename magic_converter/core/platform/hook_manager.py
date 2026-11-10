"""Nonblocking Win32 input polling, including Explorer's OLE drag loop."""

from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes
from dataclasses import dataclass
from typing import Callable

from PySide6.QtCore import QObject, QPoint, QTimer, Qt, Signal, Slot
from PySide6.QtGui import QCursor

VK_LBUTTON = 0x01
VK_SHIFT = 0x10
VK_CONTROL = 0x11
VK_ESCAPE = 0x1B


@dataclass(frozen=True)
class InputState:
    left: bool
    shift: bool
    control: bool
    x: int
    y: int
    escape: bool = False


class WindowsInput:
    def __init__(self) -> None:
        if sys.platform != "win32":
            raise OSError("전역 드래그 감지는 Windows 10/11에서만 지원합니다.")
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
        self.user32.GetAsyncKeyState.restype = ctypes.c_short
        self.user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        self.user32.GetCursorPos.restype = wintypes.BOOL

    def __call__(self) -> InputState:
        point = wintypes.POINT()
        if not self.user32.GetCursorPos(ctypes.byref(point)):
            raise ctypes.WinError(ctypes.get_last_error())
        key = self.user32.GetAsyncKeyState
        return InputState(
            bool(key(VK_LBUTTON) & 0x8000),
            bool(key(VK_SHIFT) & 0x8000),
            bool(key(VK_CONTROL) & 0x8000),
            point.x, point.y, bool(key(VK_ESCAPE) & 0x8000),
        )


class HookManager(QObject):
    wheel_requested = Signal(str, QPoint)
    dismiss_requested = Signal(str)
    error = Signal(str)

    def __init__(self, parent: QObject | None = None, *, interval_ms: int = 35,
                 threshold: int = 15, reader: Callable[[], InputState] | None = None) -> None:
        super().__init__(parent)
        self._reader = reader if reader is not None else WindowsInput()
        self.threshold = threshold
        self._origin: tuple[int, int] | None = None
        self._dragging = False
        self._mode: str | None = None
        self._consumed = False
        self._wheel_pending = False
        self.timer = QTimer(self)
        self.timer.setTimerType(Qt.TimerType.PreciseTimer)
        self.timer.setInterval(max(30, min(50, interval_ms)))
        self.timer.timeout.connect(self.poll)

    @property
    def enabled(self) -> bool:
        return self.timer.isActive()

    @Slot(bool)
    def set_enabled(self, enabled: bool) -> None:
        if enabled:
            self.timer.start()
        else:
            self.timer.stop()
            self._origin = None
            self._dragging = False
            self._mode = None
            self._consumed = True
            self._wheel_pending = False
            self.dismiss_requested.emit("disabled")

    @Slot()
    def consume_drag(self) -> None:
        """Do not reopen between dropEvent and the next polling tick."""
        self._consumed = True
        self._wheel_pending = False
        self._mode = None

    @Slot()
    def poll(self) -> None:
        try:
            state = self._reader()
        except OSError as exc:
            self.set_enabled(False)
            self.error.emit(f"입력 감지를 중지했습니다: {exc}")
            return
        if not state.left:
            if self._mode is not None or self._wheel_pending:
                self.dismiss_requested.emit("mouse_released")
            self._origin = None
            self._dragging = False
            self._mode = None
            self._consumed = False
            self._wheel_pending = False
            return
        if state.escape:
            self.consume_drag()
            self.dismiss_requested.emit("escape")
            return
        if self._origin is None:
            self._origin = (state.x, state.y)
        dx, dy = state.x - self._origin[0], state.y - self._origin[1]
        self._dragging |= dx * dx + dy * dy >= self.threshold * self.threshold
        mode = "action" if state.control else "convert" if state.shift else None
        if mode is None:
            if self._mode is not None:
                self.dismiss_requested.emit("modifier_released")
            self._mode = None
            return
        if self._dragging and not self._consumed and mode != self._mode:
            self._mode = mode
            self._wheel_pending = True
            # Native coordinates are used ONLY for distance. Qt logical
            # coordinates keep the overlay centered under mixed-monitor DPI.
            self.wheel_requested.emit(mode, QCursor.pos())
