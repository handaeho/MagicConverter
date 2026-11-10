"""Human-only provider timing probe. Never deletes or edits a source archive.

Uses generated files and a persistent Qt target, without the global wheel hook.
An enter probe must be cancelled with Escape; a drop probe reads on Drop only.
"""
from __future__ import annotations

import argparse
import datetime
import hashlib
import json
from pathlib import Path
import sys
import time
import uuid
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.capture_zip_session import fixtures, sha


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--read-phase', choices=['enter', 'drop'], required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    assert root.is_relative_to(ROOT / '.test-artifacts')
    root.mkdir(parents=True, exist_ok=False)
    session = uuid.uuid4().hex[:4].upper()
    expected = fixtures(session)
    archive = root / ('PROBE-' + session + '.zip')
    with zipfile.ZipFile(archive, 'w', compression=zipfile.ZIP_DEFLATED) as zipped:
        for name, data in expected.items():
            zipped.writestr(name, data)
    initial = sha(archive)
    started = time.monotonic()
    report = dict(status='awaiting_human', session=session, read_phase=args.read_phase,
                  source_application='Bandizip 7.47.0.1 (operator confirmation required)',
                  archive=str(archive), archive_initial_sha256=initial, archive_intact=True,
                  events=[], session_open=True, global_wheel=False,
                  source_identity='Not PID authenticated', synthesized_input=False)

    def record(kind, **values):
        report['events'].append(dict(event=kind, elapsed=time.monotonic() - started,
            utc=datetime.datetime.now(datetime.timezone.utc).isoformat(), **values))
        save()

    def save():
        temporary = root / 'report.tmp'
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
        temporary.replace(root / 'report.json')

    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QVBoxLayout, QWidget
    from magic_converter.core.automation.web_inputs import from_mime

    class Target(QWidget):
        def __init__(self):
            super().__init__()
            self.setWindowTitle('반디집 진단 ' + session + ' · ' + args.read_phase)
            self.resize(820, 400)
            self.setAcceptDrops(True)
            layout = QVBoxLayout(self)
            text = ('이 창 안으로 Shift 드래그 → 3초 멈춤 → Esc로 취소 (놓지 마세요)'
                    if args.read_phase == 'enter' else '이 창에 Shift 드래그하여 놓습니다')
            layout.addWidget(QLabel(text + '\n고정 수신기 대조입니다. 실제 변환 앱/휠 검사가 아닙니다.'))
            path = QLineEdit(str(archive)); path.setReadOnly(True); layout.addWidget(path)
            layout.addWidget(QLabel('반디집으로 이 ZIP을 열고 ' + next(iter(expected)) + ' 하나만 사용합니다.'))
            self.status = QLabel('대기'); layout.addWidget(self.status)
            self.timer = QTimer(self); self.timer.timeout.connect(self.poll); self.timer.start(100)
            QTimer.singleShot(20 * 60 * 1000, self.close)

        def effect(self, event):
            if event.possibleActions() & Qt.DropAction.CopyAction:
                event.setDropAction(Qt.DropAction.CopyAction); event.accept()
            else:
                event.ignore()

        def read(self, event):
            record('before_mime_read', archive_sha256=sha(archive))
            try:
                paths, web = from_mime(event.mimeData())
                record('after_mime_read', local_paths=[str(p) for p in paths] if not web else [],
                       virtual_names=[w.name for w in web], archive_sha256=sha(archive))
                if web:
                    values = {w.name: w.data for w in web}
                else:
                    values = {p.name: p.read_bytes() for p in paths if p.is_file() and p.stat().st_size <= 32 * 1024**2}
                matched = bool(values) and all(expected.get(n) == data for n, data in values.items())
                record('fixture_read', fixture_matches=matched,
                       sha256={n: hashlib.sha256(v).hexdigest() for n, v in values.items()})
                return matched
            except Exception as error:
                record('read_error', error=type(error).__name__ + ': ' + str(error))
                return False

        def dragEnterEvent(self, event):
            self.effect(event)
            record('enter', possible=event.possibleActions().value, proposed=event.proposedAction().value,
                   accepted=event.isAccepted(), effect=event.dropAction().value, modifiers=event.modifiers().value,
                   formats=list(event.mimeData().formats()))
            if args.read_phase == 'enter':
                self.read(event)

        def dragMoveEvent(self, event):
            self.effect(event)

        def dragLeaveEvent(self, event):
            record('leave'); event.accept()
            report['status'] = 'cancelled_or_left'; self.poll()

        def dropEvent(self, event):
            self.effect(event)
            record('drop', possible=event.possibleActions().value, proposed=event.proposedAction().value,
                   effect=event.dropAction().value, modifiers=event.modifiers().value)
            matched = self.read(event)
            if not matched:
                event.ignore()
            record('drop_return', accepted=event.isAccepted(), effect=event.dropAction().value)
            report['status'] = 'matched_drop' if matched else 'failed_drop'

        def poll(self):
            current = archive.exists() and sha(archive) == initial
            if current != report['archive_intact']:
                report['archive_intact'] = current
                record('archive_changed', current_sha256=sha(archive) if archive.exists() else None)
            self.status.setText('ZIP 유지=' + str(current) + ' / 상태=' + report['status'])
            save()
            if (root / 'STOP').exists():
                self.close()

        def closeEvent(self, event):
            report['session_open'] = False
            report['final_archive_sha256'] = sha(archive) if archive.exists() else None
            save(); event.accept()

    app = QApplication([]); target = Target(); target.show(); save()
    return app.exec()


if __name__ == '__main__':
    raise SystemExit(main())
