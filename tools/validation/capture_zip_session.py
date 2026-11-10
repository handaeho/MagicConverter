"""Human ZIP OLE session with fresh files and a live final-GUI receipt monitor.

No mouse/keyboard input is synthesized. The GUI control uses supported IPC and
is recorded separately from human drops. Never modifies a sealed release.
"""
from __future__ import annotations

import argparse
from dataclasses import replace
import datetime
import hashlib
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from PIL import Image
from PIL.PngImagePlugin import PngInfo


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fixtures(session):
    result = {}
    for index, label in enumerate(['single', 'pair-a', 'pair-b']):
        digest = hashlib.sha256((session + label).encode()).digest()
        color = tuple(40 + c % 170 for c in digest[:3])
        image = Image.new('RGB', (96, 64), color)
        info = PngInfo(); info.add_text('zip_validation_session', session)
        data = io.BytesIO(); image.save(data, 'PNG', pnginfo=info)
        result[f'{label}-{session}.png'] = data.getvalue()
    return result


def case_for_names(names, expected):
    names = set(names)
    ordered = list(expected)
    return 'single' if names == {ordered[0]} else 'multiple' if names == set(ordered[1:]) else 'unscoped'


def gui_receipts(history, expected, preset_id, output):
    """Require exact fixture groups, current preset, completed jobs and pixels."""
    result = {}
    for entry in history.get('entries', []):
        if entry.get('preset_id') != preset_id:
            continue
        files = entry.get('files', [])
        case = case_for_names([Path(f['source']).name for f in files], expected)
        if case == 'unscoped':
            continue
        record = {'passed': False, 'entry_id': entry['id'], 'files': []}
        try:
            assert entry['action_key'] == 'image:bmp'
            assert len(files) == (1 if case == 'single' else 2)
            for item in files:
                name = Path(item['source']).name
                assert item['state'] == 'done' and not item['errors'] and not item['warnings']
                assert len(item['outputs']) == 1
                path = Path(item['outputs'][0]).resolve()
                assert path.parent == output.resolve()
                with Image.open(io.BytesIO(expected[name])) as original, Image.open(path) as converted:
                    assert converted.format == 'BMP'
                    assert converted.size == original.size
                    assert converted.convert('RGB').tobytes() == original.convert('RGB').tobytes()
                record['files'].append({'source': item['source'], 'output': str(path), 'sha256': sha(path)})
            record['passed'] = True
        except (AssertionError, KeyError, OSError) as error:
            record['error'] = type(error).__name__ + ': ' + str(error)
        result[case] = record  # Latest attempt controls each case.
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path, required=True)
    parser.add_argument('--expected-sha256', required=True)
    parser.add_argument('--gui-only', action='store_true', help='Skip receiver cases; never count them as passed')
    parser.add_argument('--qt-mime-trace', action='store_true', help='Enable final EXE native Qt OLE diagnostics')
    parser.add_argument('--source-application', default='unspecified', help='Expected human source; not PID authentication')
    parser.add_argument('--release-modifier-inside', action='store_true', help='Opt in only for this isolated validation profile')
    parser.add_argument('--source-drag-trace', action='store_true', help='Observe source GUI instead of EXE; never frozen evidence')
    parser.add_argument('--keep-on-archive-change', action='store_true', help='Diagnostic generated ZIP only: record failure but await explicit Stop')
    args = parser.parse_args()
    root = args.output.resolve()
    assert root.is_relative_to(ROOT / '.test-artifacts')
    root.mkdir(parents=True, exist_ok=False)
    exe = args.executable.resolve()
    assert sha(exe) == args.expected_sha256.lower()
    session = uuid.uuid4().hex[:4].upper()
    expected = fixtures(session)
    archive = root / f'ZIP-{session}.zip'
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, data in expected.items():
            zipped.writestr(name, data)
    with zipfile.ZipFile(archive) as zipped:
        assert zipped.testzip() is None
        assert {n: zipped.read(n) for n in zipped.namelist()} == expected
    archive_hash = sha(archive)
    profile = root / 'app-profile'; profile.mkdir()
    output = root / 'bmp-results'; output.mkdir()
    controls = root / 'api-control'; controls.mkdir()
    control_source = controls / f'control-{session}.png'
    Image.new('RGB', (17, 13), (23, 91, 177)).save(control_source)
    from magic_converter import __version__
    from magic_converter.core.settings.preferences import default_preferences, Preset, WheelProfile, OutputRule
    from magic_converter.core.settings.settings_store import SettingsStore
    prefs = default_preferences()
    preset = Preset('zip-' + session, 'ZIP ' + session, 'image:bmp', output=OutputRule('folder', str(output)))
    wheel = WheelProfile('zip-' + session, 'ZIP ' + session, 'convert', (preset.id,))
    SettingsStore(profile / 'settings.json').save(replace(prefs, presets=(*prefs.presets, preset),
        profiles=(*prefs.profiles, wheel), convert_profile=wheel.id, output=preset.output,
        release_modifier_inside=args.release_modifier_inside))
    report = {'status': 'awaiting_receiver', 'version': __version__, 'session': session,
        'executable': str(exe), 'executable_sha256': sha(exe), 'profile': str(profile),
        'archive': str(archive), 'archive_initial_sha256': archive_hash,
        'fixtures': {n: hashlib.sha256(d).hexdigest() for n, d in expected.items()},
        'events': [], 'receiver_cases': {}, 'gui_cases': {}, 'api_control': {'status': 'not_started'},
        'archive_intact': True, 'session_open': True, 'firefox': 'excluded_by_user',
        'gui_only': args.gui_only, 'qt_mime_trace': args.qt_mime_trace,
        'source_application': args.source_application, 'source_identity_evidence': 'operator must confirm; not PID-authenticated',
        'release_modifier_inside': args.release_modifier_inside,
        'frozen_gui': not args.source_drag_trace,
        'gui_execution_kind': 'source_with_observers' if args.source_drag_trace else 'frozen_executable',
        'keep_on_archive_change': args.keep_on_archive_change,
        'limits': ['Receiver and final-GUI cases are separate.', 'Offered MIME is not supplier-PID authentication.',
                   'IPC control is not OLE evidence. Human source application must be recorded separately.']}
    def save():
        report['updated_utc'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
        temporary = root / 'report.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(root / 'report.json')

    os.environ.pop('QT_QPA_PLATFORM', None)
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QPushButton, QVBoxLayout, QWidget
    from magic_converter.core.automation.local_ipc import call
    from magic_converter.core.automation.web_inputs import from_mime
    from magic_converter.core.jobs.process_runner import stop_process

    class Monitor(QWidget):
        def __init__(self):
            super().__init__()
            self.process = None; self.instance = None; self.control_sent = False
            self.closing = False; self.started = None; self.phase = 'receiver'; self.last_history = None
            self.setWindowTitle(f'ZIP 검사 제어판 {session} · Firefox 제외')
            self.setAcceptDrops(True); self.resize(850, 470)
            layout = QVBoxLayout(self)
            self.instructions = QLabel('1단계: ZIP 안 PNG를 이 창으로 드롭합니다. Shift를 누르지 마세요.\n'
                '압축 풀기·PNG 열기 없이 파일 목록의 항목을 잡아 옮깁니다.\n'
                + list(expected)[0] + ' 하나 → ' + ' · '.join(list(expected)[1:]) + ' 두 개 함께')
            self.instructions.setWordWrap(True); layout.addWidget(self.instructions)
            path = QLineEdit(str(archive)); path.setReadOnly(True); layout.addWidget(path)
            self.live = QLabel(); self.live.setWordWrap(True); layout.addWidget(self.live)
            self.drop_note = QLabel('여기가 수신기 드롭 영역입니다.'); self.drop_note.setWordWrap(True); layout.addWidget(self.drop_note)
            self.stop_button = QPushButton('검사 앱 종료 및 결과 저장')
            self.stop_button.clicked.connect(self.finish); layout.addWidget(self.stop_button)
            self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(700)
            QTimer.singleShot(30 * 60 * 1000, self.finish)
            self.poll()

        def dragEnterEvent(self, event):
            if event.possibleActions() & Qt.DropAction.CopyAction:
                event.setDropAction(Qt.DropAction.CopyAction); event.accept()
            else:
                event.ignore()

        dragMoveEvent = dragEnterEvent

        def dropEvent(self, event):
            item = {'phase': self.phase, 'utc': datetime.datetime.now(datetime.timezone.utc).isoformat(),
                'formats': list(event.mimeData().formats()), 'external_qt_source': event.source() is None,
                'possible_actions': event.possibleActions().value, 'proposed_action': event.proposedAction().value,
                'modifiers': event.modifiers().value, 'fixture_matches': False}
            try:
                paths, web = from_mime(event.mimeData())
                item['local_paths'] = [str(p) for p in paths] if not web else []
                names = [w.name for w in web] if web else [p.name for p in paths]
                item['names'] = names; item['case'] = case_for_names(names, expected)
                if item['case'] != 'unscoped':
                    assert len(names) == (1 if item['case'] == 'single' else 2)
                    values = {w.name: w.data for w in web} if web else {p.name: p.read_bytes() for p in paths if p.stat().st_size <= 32*1024**2}
                    item['fixture_matches'] = all(values.get(n) == expected[n] for n in names)
                    item['content_sha256'] = {n: hashlib.sha256(d).hexdigest() for n, d in values.items()}
                    item['adapter'] = 'web data' if web else 'local paths'
                assert item['fixture_matches'] and event.possibleActions() & Qt.DropAction.CopyAction
                event.setDropAction(Qt.DropAction.CopyAction); event.accept()
                item['accepted_action'] = event.dropAction().value
            except Exception as error:
                item['error'] = type(error).__name__ + ': ' + str(error); event.ignore()
            report['events'].append(item)
            if self.phase == 'receiver' and item.get('case') in {'single', 'multiple'}:
                report['receiver_cases'][item['case']] = item['fixture_matches']
            self.drop_note.setText(('수신기 결과: ' if self.phase == 'receiver' else '휠 대신 제어판에 놓였습니다: ')
                + item.get('case', 'error') + ' / 파일 일치=' + str(item['fixture_matches']))
            save(); self.poll()

        def start_gui(self):
            env = os.environ.copy()
            for key in ['QT_QPA_PLATFORM', 'MAGIC_CONVERTER_PROFILE_DIR']:
                env.pop(key, None)
            if args.qt_mime_trace:
                env['QT_LOGGING_RULES'] = 'qt.qpa.mime.debug=true'
                env['QT_FORCE_STDERR_LOGGING'] = '1'
                env['QT_MESSAGE_PATTERN'] = '[%{time process} %{pid} %{type} %{category}] %{message}'
            self.log = (root / 'app-process.log').open('wb')
            command = ([sys.executable, '-B', str(ROOT / 'tools/validation/trace_wheel_input.py'),
                        '--trace', str(root / 'input-lifetime.jsonl')] if args.source_drag_trace else [str(exe)])
            report['gui_command'] = command
            self.process = subprocess.Popen([*command, '--tray', '--data-dir', str(profile)], env=env,
                stdout=self.log, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NO_WINDOW)
            self.started = time.monotonic(); self.phase = 'starting_gui'
            report['launcher_pid'] = self.process.pid

        def poll(self):
            report['archive_intact'] = archive.exists() and sha(archive) == archive_hash
            if not report['archive_intact']:
                report['failure'] = 'source_archive_changed'
                if not args.keep_on_archive_change:
                    self.closing = True
            if (root / 'STOP').exists():
                self.closing = True
            if self.phase == 'receiver' and not self.closing and (args.gui_only or
                    all(report['receiver_cases'].get(c) for c in ['single', 'multiple'])):
                self.start_gui()
            if self.process is not None:
                report['app_exit'] = self.process.poll()
                if self.process.poll() is None and self.instance is None:
                    try:
                        response = call(profile, {'command': 'status'}, timeout=.3)
                        assert response['ok']; self.instance = response['instance']; report['instance'] = self.instance
                    except (OSError, ValueError, TimeoutError, AssertionError):
                        if time.monotonic() - self.started > 60:
                            report['startup_error'] = 'No IPC instance after 60 seconds'; self.finish()
                if self.instance and not self.control_sent and not self.closing:
                    try:
                        response = call(profile, {'id': 'zip-control-' + session, 'command': 'send', 'paths': [str(control_source)], 'recipe': {
                            'id': 'api-' + session, 'name': 'API 대조 ' + session, 'action_key': 'image:bmp',
                            'output': {'location': 'folder', 'directory': str(controls / 'output')}}}, timeout=1)
                        assert response['ok']; self.control_sent = True
                        report['api_control'] = {'status': 'awaiting_output', 'response': response}
                    except (OSError, ValueError, TimeoutError, AssertionError) as error:
                        report['api_control'] = {'status': 'error', 'error': str(error)}
                history_path = profile / 'history.json'
                if history_path.exists():
                    try:
                        history = json.loads(history_path.read_text(encoding='utf-8'))
                        report['gui_cases'] = gui_receipts(history, expected, preset.id, output)
                        for entry in history.get('entries', []):
                            if entry.get('preset_id') == 'api-' + session:
                                f = entry['files'][0]; target = Path(f['outputs'][0])
                                with Image.open(target) as image:
                                    assert f['state'] == 'done' and not f['errors'] and image.size == (17, 13)
                                    assert image.convert('RGB').tobytes() == bytes((23,91,177))*17*13
                                report['api_control'].update(status='passed', entry_id=entry['id'], output=str(target), sha256=sha(target))
                        if report['api_control']['status'] == 'passed' and self.phase == 'starting_gui':
                            self.phase = 'gui'
                    except (OSError, ValueError, KeyError, IndexError, AssertionError) as error:
                        report['history_error'] = type(error).__name__ + ': ' + str(error)
            ready = all(report['gui_cases'].get(c, {}).get('passed') for c in ['single', 'multiple'])
            if self.closing and self.process is not None and self.process.poll() is None and self.instance:
                try: call(profile, {'command': 'stop', 'instance': self.instance}, timeout=.3)
                except (OSError, TimeoutError): pass
            report['status'] = ('failed' if report.get('failure') else
                ('complete_source_gui_records' if args.source_drag_trace else
                 'complete_gui_records' if args.gui_only else 'complete_machine_records')
                if ready and report['api_control']['status'] == 'passed'
                and (args.gui_only or all(report['receiver_cases'].get(c) for c in ['single', 'multiple']))
                and report.get('app_exit') == 0 and report['archive_intact']
                else 'awaiting_gui' if self.phase == 'gui' else self.phase)
            self.instructions.setText(('2단계: 같은 ZIP 안 PNG를 Shift 드래그 → 휠 위쪽 ZIP ' + session + '\n'
                + ('슬롯 안에서 Shift만 먼저 놓습니다. ‘이제 놓으세요’와 활성 슬롯을 확인한 뒤 마우스를 놓습니다.\n' if args.release_modifier_inside else '')
                + '단일 1개, pair 두 개를 각각 변환하세요. 아래 실제 앱 접수 표시를 확인한 뒤 종료합니다.')
                if self.phase == 'gui' else self.instructions.text())
            receiver = ('수신기 검사는 생략합니다. 실제 휠 결과를 아래에서 확인하세요.' if args.gui_only else
                '수신 단일=' + str(report['receiver_cases'].get('single'))
                + ' · 다중=' + str(report['receiver_cases'].get('multiple')))
            self.live.setText('세션 ' + session + ' / ' + receiver
                + '\n실제 앱 단일=' + str(report['gui_cases'].get('single',{}).get('passed',False))
                + ' · 실제 앱 다중=' + str(report['gui_cases'].get('multiple',{}).get('passed',False))
                + '\nZIP 원본 유지=' + str(report['archive_intact']) + ' / 앱 종료=' + str(report.get('app_exit'))
                + '\nAPI 대조=' + report['api_control']['status'])
            save()
            if self.closing and (self.process is None or self.process.poll() is not None):
                self.timer.stop(); QApplication.instance().quit()

        def finish(self):
            self.closing = True; self.phase = 'closing' if self.phase != 'gui' else self.phase
            if self.process is not None and self.process.poll() is None and self.instance is None:
                stop_process(self.process, None); report['forced_owned_startup_cleanup'] = True
            self.poll()

        def closeEvent(self, event):
            event.ignore(); self.finish()

    app = QApplication.instance() or QApplication([])
    app.setQuitOnLastWindowClosed(False)
    window = Monitor(); window.show(); save()
    code = app.exec()
    report['session_open'] = False
    if not report.get('failure') and not report['status'].startswith('complete_'):
        report['status'] = 'incomplete_source_gui_records' if args.source_drag_trace else 'incomplete_gui_records'
    if window.process is not None:
        report['app_exit'] = window.process.poll(); window.log.close()
    save()
    return code


if __name__ == '__main__':
    raise SystemExit(main())
