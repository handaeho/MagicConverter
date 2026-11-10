"""Synthetic Qt controls for the verifier, never native browser evidence."""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
from PySide6.QtGui import QColor, QDragEnterEvent, QDragMoveEvent, QDropEvent, QImage
from PySide6.QtWidgets import QApplication
from tools.validation import capture_browser_ole as receiver

APP = QApplication.instance() or QApplication([])
BASE = 'http://127.0.0.1:50377/test-token/'
URL = BASE + 'image.png'


def report(mode='clipboard'):
    return {'version': 'test', 'browser': 'chrome', 'browser_version': '155.0.8059.40',
            'fixture_url': BASE + 'index.html', 'transport_mode': mode, 'events': [],
            'receiver_elevated': False, 'required_cases': dict(receiver.REQUIRED_CASES)}


def mime_for(case):
    mime = QMimeData()
    if case == 'link':
        mime.setText(URL)
    elif case == 'image':
        image = QImage(96, 64, QImage.Format.Format_ARGB32)
        image.fill(QColor(24, 128, 220))
        mime.setImageData(image)
    elif case == 'body-image':
        mime.setHtml('<p>Selected body</p><img src="' + URL + '">')
    else:
        mime.setHtml('<img src="' + URL + '"><img src="' + URL + '">')
    return mime


def all_events(mode):
    return [receiver.capture_event(mime_for(case), case, receiver.TRANSPORTS[mode], BASE, mode)
            for case in receiver.REQUIRED_CASES]


class BrowserReceiverTests(unittest.TestCase):
    def test_exact_plain_fixture_url_is_recognized_without_logging_contents(self):
        event = receiver.capture_event(mime_for('link'), 'link', 'clipboard', BASE, 'clipboard')
        self.assertTrue(event['accepted'] and event['fixture_scope'] and event['matches_expected'])
        self.assertNotIn(URL, json.dumps(event))
        for value in (BASE + 'index.html', URL + '?different=1',
                      'https://example.invalid/?next=' + URL, URL + '/other'):
            mime = QMimeData(); mime.setText(value)
            event = receiver.capture_event(mime, 'link', 'clipboard', BASE, 'clipboard')
            self.assertFalse(event['fixture_scope'] or event['matches_expected'])

    def test_four_controls_include_actual_pixel_and_rejection_counterparts(self):
        for case, expected in receiver.REQUIRED_CASES.items():
            with self.subTest(case=case):
                event = receiver.capture_event(mime_for(case), case, 'clipboard', BASE, 'clipboard')
                self.assertIs(event['accepted'], expected)
                self.assertTrue(event['fixture_scope'] and event['matches_expected'])
        mime = mime_for('image')
        image = QImage(96, 64, QImage.Format.Format_RGB32); image.fill(QColor('red'))
        mime.setImageData(image)
        self.assertFalse(receiver.capture_event(mime, 'image', 'clipboard', BASE, 'clipboard')['fixture_scope'])

    def test_foreign_or_mixed_html_resources_are_not_fixture_evidence(self):
        for html in ('<img src="' + URL + '"><img src="https://example.invalid/other.png">',
                     '<p>' + URL + '</p>', '<img src="' + URL + '?fake=1">'):
            mime = QMimeData(); mime.setHtml(html)
            event = receiver.capture_event(mime, 'multiple-images', 'clipboard', BASE, 'clipboard')
            self.assertFalse(event['fixture_scope'] or event['matches_expected'])

    def test_transport_completion_cannot_be_filled_by_another_transport(self):
        for mode, other in [('clipboard', 'drag'), ('drag', 'clipboard')]:
            data = report(mode); data['events'] = all_events(other)
            self.assertEqual(receiver.completion_status(data), 'incomplete')
            self.assertEqual(set(receiver.case_results(data).values()), {'pending'})
            data['events'] += all_events(mode)
            self.assertEqual(receiver.completion_status(data), 'passed')
        event = receiver.capture_event(mime_for('link'), 'link', 'QDropEvent', BASE, 'clipboard')
        self.assertFalse(event['matches_expected'])

    def test_last_failure_or_unscoped_attempt_does_not_reuse_earlier_pass(self):
        data = report(); data['events'] = all_events('clipboard')
        self.assertEqual(receiver.completion_status(data), 'passed')
        with patch.object(receiver, 'from_mime', return_value=((), ())):
            failure = receiver.capture_event(mime_for('link'), 'link', 'clipboard', BASE, 'clipboard')
        self.assertTrue(failure['fixture_scope'])
        self.assertFalse(failure['accepted'])
        data['events'].append(failure)
        self.assertEqual(receiver.completion_status(data), 'failed')
        data['events'].append(receiver.capture_event(mime_for('link'), 'link', 'clipboard', BASE, 'clipboard'))
        self.assertEqual(receiver.completion_status(data), 'passed')
        self.assertIn(failure, data['events'])
        event = copy.deepcopy(data['events'][-1]); event['fixture_scope'] = False
        data['events'].append(event)
        self.assertEqual(receiver.completion_status(data), 'incomplete')

    def test_missing_placeholder_or_invalid_version_never_passes(self):
        data = report(); data['events'] = all_events('clipboard')
        for version in ('unrecorded', '', 'INSTALLED_VERSION', '브라우저 정보 화면에서 확인한 버전', '1', '1.2.3.4.5'):
            data['browser_version'] = version
            self.assertFalse(receiver.valid_browser_version(version))
            self.assertEqual(receiver.completion_status(data), 'incomplete')
        for version in ('155.0.8059.40', '144.0', '144.0.1'):
            self.assertTrue(receiver.valid_browser_version(version))

    def test_empty_or_modified_required_cases_never_certify_a_pass(self):
        for required in ({}, {'link': True}, {**receiver.REQUIRED_CASES, 'image': False}):
            data = report(); data['events'] = all_events('clipboard'); data['required_cases'] = required
            self.assertEqual(receiver.completion_status(data), 'incomplete')

    def test_portable_diagnostic_catches_a_missing_frozen_parser_branch(self):
        from magic_converter.core.diagnostics.automation_diagnostics import check_plain_text_urls
        self.assertEqual(check_plain_text_urls(), {'accepted': 1, 'refused': 6})
        with patch('magic_converter.core.automation.web_inputs.from_mime', return_value=((), ())):
            with self.assertRaisesRegex(RuntimeError, 'text/plain'):
                check_plain_text_urls()

    def test_preparation_creates_no_native_pass_and_refuses_reusing_output(self):
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder) / 'prepared'
            command = [sys.executable, '-B', str(Path(receiver.__file__)), '--browser', 'chrome',
                       '--browser-version', '155.0.8059.40', '--transport', 'clipboard',
                       '--prepare-only', '--output', str(output)]
            result = subprocess.run(command, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            before = (output / 'report.json').read_bytes()
            data = json.loads(before)
            self.assertEqual(data['status'], 'prepared')
            self.assertEqual(data['events'], [])
            self.assertFalse(data['session_open'])
            self.assertEqual(data['transport_mode'], 'clipboard')
            self.assertEqual(receiver.completion_status(data), 'incomplete')
            self.assertNotEqual(subprocess.run(command, capture_output=True, timeout=20).returncode, 0)
            self.assertEqual((output / 'report.json').read_bytes(), before)

    def test_drag_zone_uses_copy_and_refuses_move_only(self):
        data = report('drag'); saved = Mock()
        window = receiver.create_receiver(data, BASE, saved, 10)
        self.addCleanup(window.deleteLater)
        self.assertTrue(window.drop_area.acceptDrops())
        self.assertFalse(window.clipboard_button.isEnabled())
        mime = mime_for('link')
        enter = QDragEnterEvent(QPoint(20, 20), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                               mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier)
        window.drop_area.dragEnterEvent(enter)
        self.assertTrue(enter.isAccepted())
        self.assertEqual(enter.dropAction(), Qt.DropAction.CopyAction)
        move = QDragMoveEvent(QPoint(20, 20), Qt.DropAction.CopyAction | Qt.DropAction.MoveAction,
                             mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        window.drop_area.dragMoveEvent(move)
        self.assertTrue(move.isAccepted())
        drop = QDropEvent(QPointF(20, 20), Qt.DropAction.CopyAction, mime,
                          Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        window.drop_area.dropEvent(drop)
        self.assertEqual(len(data['events']), 1)
        saved.assert_called_once()
        denied = QDropEvent(QPointF(20, 20), Qt.DropAction.MoveAction, mime,
                            Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier)
        window.drop_area.dropEvent(denied)
        self.assertFalse(denied.isAccepted())
        self.assertEqual(len(data['events']), 1)

    def test_clipboard_mode_shows_steps_progress_and_elevation_warning(self):
        data = report(); data['receiver_elevated'] = True
        window = receiver.create_receiver(data, BASE, Mock(), 10)
        self.addCleanup(window.deleteLater)
        self.assertFalse(window.drop_area.acceptDrops())
        self.assertTrue(window.clipboard_button.isEnabled())
        self.assertIn('링크 주소 복사', window.instructions.text())
        self.assertIn('관리자 권한', window.progress.text())
        window.capture(mime_for('link'), 'clipboard')
        self.assertIn('1/4', window.progress.text())
        window.case.setCurrentText('body-image')
        self.assertIn('Ctrl+C', window.instructions.text())
