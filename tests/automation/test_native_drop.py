"""Optional modifier release and immutable temporary provider inputs."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from dataclasses import replace
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtWidgets import QApplication
from magic_converter.core.settings.preferences import Preferences, SettingsError, default_preferences
from magic_converter.ui.shell.radial_wheel import RadialWheelWindow

APP = QApplication.instance() or QApplication([])


class NativeDropTests(unittest.TestCase):
    def test_release_is_opt_in_and_roundtrips(self):
        self.assertFalse(Preferences().release_modifier_inside)
        prefs = replace(default_preferences(), release_modifier_inside=True)
        self.assertTrue(Preferences.from_dict(prefs.to_dict()).release_modifier_inside)
        with self.assertRaises(SettingsError):
            replace(prefs, release_modifier_inside=1)

    def test_legacy_release_cancels_but_opt_in_keeps_target_until_drop(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'sample.png';path.write_bytes(b'fixture')
            for enabled in [False, True]:
                with self.subTest(enabled=enabled):
                    prefs=replace(default_preferences(), release_modifier_inside=enabled)
                    wheel=RadialWheelWindow(lambda:False, preferences=lambda:prefs)
                    wheel.show_for_drag('convert',QPoint(500,500))
                    mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))])
                    wheel.dragEnterEvent(QDragEnterEvent(QPoint(160,60),Qt.CopyAction,mime,Qt.LeftButton,Qt.ShiftModifier))
                    wheel.request_dismiss('modifier_released')
                    self.assertEqual(wheel.isVisible(),enabled)
                    if enabled:
                        wheel.dragMoveEvent(QDragMoveEvent(QPoint(160,60),Qt.CopyAction,mime,Qt.LeftButton,Qt.NoModifier))
                        wheel.request_dismiss('mouse_released')
                        self.assertTrue(wheel._release_timer.isActive())
                        self.assertEqual(wheel._release_timer.interval(),30_000)
                        selected=[];wheel.request_dropped.connect(selected.append)
                        drop=QDropEvent(QPointF(160,60),Qt.CopyAction,mime,Qt.NoButton,Qt.NoModifier)
                        wheel.dropEvent(drop)
                        self.assertEqual(len(selected),1)
                        self.assertTrue(selected[0].web_inputs)
                        self.assertEqual(selected[0].web_inputs[0].data,b'fixture')
                    wheel.dismiss();wheel.deleteLater();APP.processEvents()

    def test_temporary_bytes_survive_provider_deletion_and_limits_precede_read(self):
        from magic_converter.core.automation.native_drop import snapshot_temporary
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'image.png';path.write_bytes(b'pixels')
            paths,items=snapshot_temporary((path,),temp_root=root)
            path.unlink()
            self.assertEqual(items[0].data,b'pixels')
            self.assertEqual(paths,(items[0].placeholder,))
            path.write_bytes(b'12345')
            with patch.object(Path,'open',side_effect=AssertionError('must not read oversized input')):
                with self.assertRaisesRegex(ValueError,'32 MiB'):
                    snapshot_temporary((path,),temp_root=root,max_bytes=4)

    def test_opt_in_requires_key_release_before_unmodified_native_drop(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'sample.png';path.write_bytes(b'fixture')
            for released,modifiers,buttons,expected in [(False,Qt.NoModifier,Qt.NoButton,False),
                                                (False,Qt.ShiftModifier,Qt.LeftButton,False),
                                                (False,Qt.NoModifier,Qt.LeftButton,True),
                                                (True,Qt.ShiftModifier,Qt.LeftButton,False),
                                                (True,Qt.NoModifier,Qt.LeftButton,True)]:
                with self.subTest(released=released,modifiers=modifiers):
                    prefs=replace(default_preferences(),release_modifier_inside=True)
                    wheel=RadialWheelWindow(lambda:False,preferences=lambda:prefs)
                    wheel.show_for_drag('convert',QPoint(500,500))
                    mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))])
                    wheel.dragEnterEvent(QDragEnterEvent(QPoint(160,60),Qt.CopyAction|Qt.MoveAction,
                        mime,Qt.LeftButton,Qt.ShiftModifier))
                    if released:wheel.request_dismiss('modifier_released')
                    move=QDragMoveEvent(QPoint(160,60),Qt.CopyAction|Qt.MoveAction,mime,buttons,modifiers)
                    wheel.dragMoveEvent(move)
                    self.assertEqual(move.isAccepted(),expected)
                    selected=[];wheel.request_dropped.connect(selected.append)
                    drop=QDropEvent(QPointF(160,60),Qt.CopyAction|Qt.MoveAction,mime,Qt.NoButton,modifiers)
                    wheel.dropEvent(drop)
                    self.assertEqual(drop.isAccepted(),expected)
                    self.assertEqual(len(selected),int(expected))
                    wheel.dismiss();wheel.deleteLater();APP.processEvents()

    def test_regular_paths_are_not_snapshotted_or_read(self):
        from magic_converter.core.automation.native_drop import snapshot_temporary
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);path=root/'outside.png';path.write_bytes(b'ordinary')
            with patch.object(Path,'open',side_effect=AssertionError('not a temporary provider file')):
                self.assertEqual(snapshot_temporary((path,),temp_root=root/'other'),((path,),()))

    def test_qt_window_translation_accepts_key_change_before_poll(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'sample.png';path.write_bytes(b'fixture')
            prefs=replace(default_preferences(),release_modifier_inside=True)
            wheel=RadialWheelWindow(lambda:False,preferences=lambda:prefs)
            wheel.show_for_drag('convert',QPoint(500,500));APP.processEvents()
            window=wheel.windowHandle();self.assertIsNotNone(window)
            mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))])
            selected=[];wheel.request_dropped.connect(selected.append)
            try:
                enter=QDragEnterEvent(QPoint(160,60),Qt.CopyAction|Qt.MoveAction,mime,Qt.LeftButton,Qt.ShiftModifier)
                QApplication.sendEvent(window,enter)
                self.assertTrue(enter.isAccepted())
                for modifiers,accepted in [(Qt.ShiftModifier,False),(Qt.NoModifier,True)]:
                    move=QDragMoveEvent(QPoint(160,60),Qt.CopyAction|Qt.MoveAction,mime,Qt.LeftButton,modifiers)
                    QApplication.sendEvent(window,move)
                    self.assertEqual(move.isAccepted(),accepted)
                    if accepted:self.assertEqual(move.dropAction(),Qt.CopyAction)
                # No polling callback has run between these Qt window events.
                self.assertTrue(wheel._ready_for_drop())
                drop=QDropEvent(QPointF(160,60),Qt.CopyAction|Qt.MoveAction,mime,Qt.NoButton,Qt.NoModifier)
                QApplication.sendEvent(window,drop)
                self.assertTrue(drop.isAccepted());self.assertEqual(len(selected),1)
                self.assertEqual(selected[0].web_inputs[0].data,b'fixture')
            finally:
                wheel.dismiss();wheel.deleteLater();APP.processEvents()

    def test_release_option_survives_settings_dialog_load_and_save(self):
        from magic_converter.ui.settings.settings_dialog import SettingsDialog
        prefs=replace(default_preferences(),release_modifier_inside=True)
        dialog=SettingsDialog(prefs.base_values(),preferences=prefs)
        self.assertTrue(dialog.values()['preferences'].release_modifier_inside)
        dialog._load_preferences(replace(prefs,release_modifier_inside=False))
        self.assertFalse(dialog.values()['preferences'].release_modifier_inside)
        dialog.deleteLater();APP.processEvents()

    def test_latched_leave_cancels_and_mouse_release_still_dismisses_hook(self):
        from PySide6.QtGui import QDragLeaveEvent
        from magic_converter.core.platform.hook_manager import HookManager,InputState
        state=[InputState(True,True,False,0,0)]
        hook=HookManager(reader=lambda:state[0]);dismiss=[];hook.dismiss_requested.connect(dismiss.append)
        hook.poll();state[0]=replace(state[0],x=100);hook.poll()
        state[0]=replace(state[0],shift=False);hook.poll()
        state[0]=replace(state[0],left=False);hook.poll()
        self.assertEqual(dismiss[-2:],['modifier_released','mouse_released'])
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'sample.png';path.write_bytes(b'fixture')
            prefs=replace(default_preferences(),release_modifier_inside=True)
            wheel=RadialWheelWindow(lambda:False,preferences=lambda:prefs)
            wheel.show_for_drag('convert',QPoint(500,500));mime=QMimeData();mime.setUrls([QUrl.fromLocalFile(str(path))])
            wheel.dragEnterEvent(QDragEnterEvent(QPoint(160,60),Qt.CopyAction,mime,Qt.LeftButton,Qt.ShiftModifier))
            wheel.request_dismiss('modifier_released');wheel.dragLeaveEvent(QDragLeaveEvent())
            self.assertFalse(wheel.isVisible())
            wheel.deleteLater();APP.processEvents()


if __name__=='__main__':
    unittest.main()
