"""Human-operated browser MIME evidence, separated by transport and fixture."""
import argparse
import functools
import io
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import re
import sys
import threading
import time
import uuid

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from PIL import Image
from magic_converter import __version__
from magic_converter.core.automation.web_inputs import from_mime, single_text_url

REQUIRED_CASES = {'link': True, 'image': True, 'body-image': False, 'multiple-images': False}
TRANSPORTS = {'drag': 'QDropEvent', 'clipboard': 'clipboard'}


def fixture_pixels(data):
    if not data or len(data) > 2 * 1024 * 1024:
        return False
    try:
        with Image.open(io.BytesIO(data)) as image:
            return (image.size == (96, 64) and image.convert('RGBA').tobytes()
                    == bytes((24, 128, 220, 255)) * 96 * 64)
    except (OSError, ValueError, Image.DecompressionBombError):
        return False


def valid_browser_version(value):
    return isinstance(value, str) and bool(re.fullmatch(r'\d{1,5}(?:\.\d{1,5}){1,3}', value))


def fixture_scope(mime, items, base):
    """Exact generated image URL/pixels only; do not retain clipboard contents."""
    expected = base + 'image.png'
    if any(i.url != expected if i.url else not fixture_pixels(i.data) for i in items):
        return False
    known = any(i.data and fixture_pixels(i.data) for i in items)
    if mime.hasUrls():
        urls = [u.toString() for u in mime.urls()]
        if not urls or any(u != expected for u in urls):
            return False
        known = True
    if mime.hasHtml():
        from bs4 import BeautifulSoup
        html = mime.html()
        if len(html) > 1_000_000:
            return False
        soup = BeautifulSoup(html, 'html.parser')
        refs = [n.get('src' if n.name == 'img' else 'href', '') for n in soup.find_all(['img', 'a'])]
        if refs:
            if any(u != expected for u in refs):
                return False
            known = True
    elif mime.hasText() and not mime.hasUrls() and not mime.hasImage():
        try:
            known = single_text_url(mime.text()) == expected
        except ValueError:
            return False
    return bool(known)


def capture_event(mime, case, transport, base, transport_mode):
    if case not in REQUIRED_CASES or transport_mode not in TRANSPORTS or transport not in TRANSPORTS.values():
        raise ValueError('Unknown capture case or transport')
    _, items = from_mime(mime)
    scoped = fixture_scope(mime, items, base)
    accepted = bool(items)
    return {'case': case, 'transport': transport,
            'formats': [str(f)[:200] for f in mime.formats()][:100],
            'accepted': accepted, 'fixture_scope': scoped,
            'matches_expected': scoped and accepted == REQUIRED_CASES[case]
                                and transport == TRANSPORTS[transport_mode]}


def case_results(report):
    expected_transport = TRANSPORTS[report['transport_mode']]
    latest = {e['case']: e for e in report['events'] if e.get('transport') == expected_transport}
    results = {}
    for case, expected in report['required_cases'].items():
        event = latest.get(case)
        if event is None:
            results[case] = 'pending'
        elif event.get('fixture_scope') is not True:
            results[case] = 'unscoped'
        else:
            results[case] = 'passed' if event.get('accepted') is expected else 'failed'
    return results


def completion_status(report):
    if report.get('required_cases') != REQUIRED_CASES or report.get('transport_mode') not in TRANSPORTS:
        return 'incomplete'
    results = case_results(report)
    if 'failed' in results.values():
        return 'failed'
    if valid_browser_version(report['browser_version']) and all(v == 'passed' for v in results.values()):
        return 'passed'
    return 'incomplete'


def process_elevated():
    """Read-only diagnostic; never change UAC, integrity or message filters."""
    if os.name != 'nt':
        return None
    try:
        import win32api
        import win32con
        import win32security
        token = win32security.OpenProcessToken(win32api.GetCurrentProcess(), win32con.TOKEN_QUERY)
        try:
            return bool(win32security.GetTokenInformation(token, win32security.TokenElevation))
        finally:
            token.Close()
    except Exception:
        return None


def create_receiver(report, base, save, seconds):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtWidgets import QComboBox, QLabel, QPushButton, QVBoxLayout, QWidget

    def accept_copy(event):
        if event.possibleActions() & Qt.DropAction.CopyAction:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
        else:
            event.ignore()

    class DropArea(QLabel):
        def dragEnterEvent(self, event):
            accept_copy(event)

        def dragMoveEvent(self, event):
            accept_copy(event)

        def dropEvent(self, event):
            accept_copy(event)
            if event.isAccepted():
                self.parentWidget().capture(event.mimeData(), 'QDropEvent')

    class Receiver(QWidget):
        def __init__(self):
            super().__init__()
            mode = report['transport_mode']
            self.setWindowTitle('Magic Converter real browser OLE validation — ' + mode)
            self.resize(760, 570)
            layout = QVBoxLayout(self)
            heading = QLabel(f"{report['browser']} {report['browser_version']} · "
                             + ('클립보드 복사 검사' if mode == 'clipboard' else '드래그 검사')
                             + '\n브라우저에서 아래 주소를 열어 검사하세요.\n' + report['fixture_url'])
            heading.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            heading.setWordWrap(True)
            layout.addWidget(heading)
            self.case = QComboBox()
            self.case.addItems(report['required_cases'])
            layout.addWidget(self.case)
            self.instructions = QLabel()
            self.instructions.setWordWrap(True)
            layout.addWidget(self.instructions)
            self.drop_area = DropArea(self)
            self.drop_area.setMinimumHeight(135)
            self.drop_area.setWordWrap(True)
            self.drop_area.setAlignment(Qt.AlignmentFlag.AlignCenter)
            self.drop_area.setStyleSheet('border: 3px dashed #2878b8; background: #edf6ff; padding: 12px;')
            self.drop_area.setAcceptDrops(mode == 'drag')
            self.drop_area.setText('브라우저의 선택 대상을 이 파란 영역에 놓으세요.' if mode == 'drag'
                                   else '브라우저에서 복사한 뒤 아래 버튼을 누르세요.')
            layout.addWidget(self.drop_area)
            self.clipboard_button = QPushButton('클립보드 읽기 / Capture copied fixture from clipboard')
            self.clipboard_button.setEnabled(mode == 'clipboard')
            self.clipboard_button.clicked.connect(lambda: self.capture(QGuiApplication.clipboard().mimeData(), 'clipboard'))
            layout.addWidget(self.clipboard_button)
            self.result = QLabel('아직 수신 기록이 없습니다.')
            self.result.setWordWrap(True)
            layout.addWidget(self.result)
            self.progress = QLabel()
            self.progress.setWordWrap(True)
            layout.addWidget(self.progress)
            self.countdown = QLabel()
            layout.addWidget(self.countdown)
            self.deadline = time.monotonic() + seconds
            self.timer = QTimer(self)
            self.timer.timeout.connect(self.tick)
            self.timer.start(1000)
            self.case.currentTextChanged.connect(self.refresh)
            self.refresh()
            self.tick()

        def tick(self):
            remaining = max(0, int(self.deadline - time.monotonic()))
            self.countdown.setText(f'남은 시간 {remaining}초 · 원문/클립보드 내용은 보고서에 저장하지 않습니다.')

        def refresh(self, *_):
            case = self.case.currentText()
            action = {'link': 'Single link control 링크', 'image': '단독 파란 그림',
                      'body-image': 'Select this body… 문장과 옆 그림을 함께 선택한 영역',
                      'multiple-images': '맨 아래 파란 그림 두 개를 함께 선택한 영역'}[case]
            if report['transport_mode'] == 'clipboard':
                how = {'link': '우클릭 → 링크 주소 복사', 'image': '우클릭 → 이미지 복사',
                       'body-image': '범위 선택 → Ctrl+C', 'multiple-images': '범위 선택 → Ctrl+C'}[case]
                text = f'대상: {action}\n{how} → 아래 클립보드 읽기 버튼'
            else:
                text = f'대상: {action}\n마우스로 잡아 파란 영역에 드롭하세요.'
            self.instructions.setText(text + ('\n기대 결과: 수락' if REQUIRED_CASES[case] else '\n기대 결과: 거부'))
            states = case_results(report)
            message = f"{sum(v == 'passed' for v in states.values())}/4종 판정 일치 · " + ', '.join(f'{k}: {v}' for k, v in states.items())
            if not valid_browser_version(report['browser_version']):
                message += '\n브라우저 버전 미기록: 전체 통과로 판정할 수 없습니다.'
            if report.get('receiver_elevated') is True:
                message += '\n관리자 권한 실행: 일반 브라우저의 드롭이 차단될 수 있습니다. 일반 터미널에서 다시 실행하세요.'
            self.progress.setText(message)

        def capture(self, mime, transport):
            event = capture_event(mime, self.case.currentText(), transport, base, report['transport_mode'])
            report['events'].append(event)
            report['case_results'] = case_results(report)
            report['status'] = completion_status(report)
            save()
            self.result.setText(f"{event['case']} · {transport} · 수락={event['accepted']} · "
                               f"시험 대상={event['fixture_scope']} · 기대 일치={event['matches_expected']}")
            self.refresh()

    return Receiver()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--browser', choices=['chrome', 'edge', 'firefox'], required=True)
    parser.add_argument('--browser-version', default='unrecorded')
    parser.add_argument('--transport', choices=TRANSPORTS, default='drag')
    parser.add_argument('--prepare-only', action='store_true')
    parser.add_argument('--seconds', type=int, default=180)
    args = parser.parse_args()
    if not 5 <= args.seconds <= 600:
        parser.error('seconds must be between 5 and 600')
    if args.browser_version != 'unrecorded' and not valid_browser_version(args.browser_version):
        parser.error('browser-version must be the actual numeric version, for example 155.0.8059.40')
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    token = uuid.uuid4().hex
    fixture = root / token
    fixture.mkdir()
    Image.new('RGB', (96, 64), (24, 128, 220)).save(fixture / 'image.png')
    (fixture / 'index.html').write_text('''<!doctype html><meta charset="utf-8"><title>Magic browser OLE control</title>
<style>body{font:24px sans-serif;padding:32px}section{margin:40px 0}</style>
<h1>Local OLE controls</h1><section><a href="image.png">Single link control</a></section>
<section><img src="image.png" alt="Single image control"></section>
<section>Select this body and the image together: <img src="image.png"></section>
<section>Multiple images: <img src="image.png"><img src="image.png"></section>''', encoding='utf-8')
    report = {'schema_version': 2, 'status': 'prepared', 'version': __version__, 'browser': args.browser,
              'browser_version': args.browser_version, 'transport_mode': args.transport,
              'session_open': False, 'receiver_elevated': process_elevated(),
              'events': [], 'required_cases': dict(REQUIRED_CASES),
              'note': 'Human labels; browser name/version are declared, not source-process authentication. '
                      'Native capture is required. Preparation/synthetic MIME is never a real browser OLE pass.'}

    def save():
        (root / 'report.json').write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')

    save()
    if args.prepare_only:
        return 0
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    class Handler(SimpleHTTPRequestHandler):
        def log_message(self, *_):
            pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), functools.partial(Handler, directory=str(root)))
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f'http://127.0.0.1:{port}/{token}/'
    report.update(status='awaiting_real_events', fixture_url=base + 'index.html', session_open=True)
    save()
    app = QApplication([])
    window = create_receiver(report, base, save, args.seconds)
    window.show()
    QTimer.singleShot(args.seconds * 1000, app.quit)
    try:
        app.exec()
    finally:
        server.shutdown()
        server.server_close()
        report.update(status=completion_status(report), session_open=False, case_results=case_results(report))
        save()
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
