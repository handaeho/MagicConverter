"""Native GUI conversion and clean shutdown via supported IPC, not OLE input.

Requires an interactive Windows session with a system tray.
"""
import argparse, hashlib, json, os, subprocess, sys, time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from PIL import Image
from PySide6.QtCore import QCoreApplication, QLockFile
from magic_converter import __version__
from magic_converter.core.automation.local_ipc import call
from magic_converter.core.jobs.process_runner import stop_process
from magic_converter.core.settings.preferences import default_preferences
from magic_converter.core.settings.settings_store import SettingsStore


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--executable', type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    app = QCoreApplication.instance() or QCoreApplication([])
    report = {'status': 'running', 'version': __version__, 'cases': [],
              'scope': 'Native GUI lifecycle via IPC; no synthetic input or OLE certification.'}
    if args.executable:
        report['executable_sha256'] = hashlib.sha256(args.executable.read_bytes()).hexdigest()
    def save():
        (root / 'report.json').write_text(json.dumps(report, indent=2) + '\n', encoding='utf-8')
    save()
    for label, tray in [('idle-tray', True), ('startup-after-conversion', False)]:
        case = {'case': label, 'status': 'running'}
        report['cases'].append(case)
        folder = root / label
        profile = folder / 'profile'
        profile.mkdir(parents=True)
        SettingsStore(profile / 'settings.json').save(default_preferences(enabled=False))
        prefix = [str(args.executable.resolve())] if args.executable else [sys.executable, str(ROOT / 'run_magic_converter.py')]
        command = prefix + ['--data-dir', str(profile)] + (['--tray'] if tray else [])
        env = {**os.environ, 'PYTHONUTF8': '1'}
        env.pop('QT_QPA_PLATFORM', None)
        with (folder / 'process.log').open('wb') as log:
            process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            try:
                status = call(profile, {'command': 'status'}, timeout=30)
                assert status['ok'], status
                if not tray:
                    colors = [(48, 168, 96), (168, 48, 96), (96, 48, 168)]
                    sources = []
                    for index, color in enumerate(colors):
                        source = folder / f'fixture-{index}.png'
                        Image.new('RGB', (96, 64), color).save(source)
                        sources.append(str(source))
                    result = call(profile, {'command': 'send', 'paths': sources,
                        'recipe': {'id': 'lifecycle', 'name': 'Lifecycle BMP', 'action_key': 'image:bmp',
                                   'output': {'location': 'folder', 'directory': str(folder / 'outputs')}}})
                    assert result['ok'], result
                    deadline = time.monotonic() + 60
                    while True:
                        status = call(profile, {'command': 'status'})
                        if status.get('recent'): break
                        assert time.monotonic() < deadline, 'Conversion did not complete'
                        time.sleep(.05)
                    history = json.loads((profile / 'history.json').read_text(encoding='utf-8'))
                    files = history['entries'][-1]['files']
                    assert len(files) == 3 and all(f['state'] == 'done' and not f['errors'] and not f['warnings'] for f in files), files
                    outputs = []
                    for file, color in zip(files, colors):
                        target = Path(file['outputs'][0])
                        with Image.open(target) as image:
                            rgb = image.convert('RGB')
                            assert rgb.size == (96, 64) and rgb.tobytes() == bytes(color) * 96 * 64
                        outputs.append({'file': str(target), 'sha256': hashlib.sha256(target.read_bytes()).hexdigest()})
                    case['verified_bmp_outputs'] = outputs
                response = call(profile, {'command': 'stop', 'instance': status['instance']})
                assert response['ok'] and response['stopping'], response
                code = process.wait(timeout=30)
                case['exit'] = code
                assert code == 0, f'Abnormal GUI exit: {code:#x}'
                lock = QLockFile(str(profile / 'magic_converter.lock'))
                assert lock.tryLock(0), 'Application lock left behind'
                lock.unlock()
                case.update(status='passed', lock_released=True)
            except Exception as error:
                case.update(status='failed', error=f'{type(error).__name__}: {error}')
            finally:
                if process.poll() is None:
                    # Restrict Windows fallback cleanup to this live Popen tree.
                    # Use the existing helper and locked pywin32, no extra deps.
                    stop_process(process, None)
                    case['forced_validation_cleanup'] = True
        save()
    report['status'] = 'passed' if all(c['status'] == 'passed' for c in report['cases']) else 'failed'
    save()
    print(json.dumps(report), flush=True)
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
