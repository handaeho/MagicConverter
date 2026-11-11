"""Build and verify a clean local Windows commit in one recorded run."""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from threading import Lock
import time
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter import __version__
from tools.validation.validation_environment import check_environment


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def git(*args):
    return subprocess.check_output(['git', *args], cwd=ROOT, text=True).strip()


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--large-workbook', type=Path, required=True)
    p.add_argument('--baseline', type=Path, required=True, help='Previous CLI executable for native recovery comparisons')
    p.add_argument('--phase-baseline', type=Path, help='Instrumented prior CLI for serial workbook comparison')
    p.add_argument('--phase-pairs', type=int, choices=(2, 3), default=3)
    p.add_argument('--excel-print-fixtures', type=Path, help='Owned Excel reference workbook and PDF directory')
    p.add_argument('--excel-print-printers', type=Path, help='Local printer selection receipt for the two-queue matrix')
    p.add_argument('--historical-workbook', type=Path, help='Directory containing historical phase and released comparisons')
    p.add_argument('--workbook-host', type=Path, help='Completed host-side comparison on the same workbook')
    p.add_argument('--workbook-benchmark', type=Path, help='Completed serial performance report for the prior CLI')
    p.add_argument('--work-path', type=Path, default=Path('build/release'))
    p.add_argument('--resume', action='store_true', help='Retry a failed suite after verifying completed build and workbook evidence')
    a = p.parse_args(); out = a.output.resolve()
    if not out.is_relative_to((ROOT / '.test-artifacts').resolve()):
        p.error('Output must stay under project .test-artifacts')
    if not a.work_path.parts or a.work_path.is_absolute() or '..' in a.work_path.parts or a.work_path.parts[0] != 'build':
        p.error('Work path must stay relative under build/')
    if sys.flags.optimize:
        p.error('Run validation without Python optimization')
    prior = None
    if a.resume:
        previous_bytes = (out / 'result.json').read_bytes()
        prior = json.loads(previous_bytes)
        if prior['status'] != 'failed' or prior['version'] != __version__:
            p.error('Resume requires a failed result for the current release')
        archive = out / 'attempts' / uuid4().hex
        archive.mkdir(parents=True, exist_ok=False)
        (archive / 'result.json').write_bytes(previous_bytes)
        if (out / 'full-suite.log').exists():
            shutil.copy2(out / 'full-suite.log', archive / 'full-suite.log')
    else:
        out.mkdir(parents=True, exist_ok=False)
    gate = Lock()
    result = {'status': 'running', 'version': __version__, 'stages': {},
              'started_utc': datetime.now(timezone.utc).isoformat(), 'scope': 'internal; no publication'}
    if prior is not None:
        result.update(prior)
        result.update(status='running', resumed_utc=datetime.now(timezone.utc).isoformat())
        result.pop('error', None); result.pop('finished_utc', None)
        result.setdefault('prior_attempts', []).append(str(archive.relative_to(out)))
    policy = ROOT / 'packaging/release-policy.json'
    release = ROOT / 'dist' / ('v' + __version__)
    cli, gui = (release / f'{name}.exe' for name in ('magic_converter-cli', 'magic_converter'))
    python = [sys.executable, '-B']
    env = os.environ.copy()
    for key in ('PYTHONPATH', 'PYTHONHOME', 'PYTHONOPTIMIZE', 'VIRTUAL_ENV', 'MAGIC_CONVERTER_FFMPEG', 'QT_PLUGIN_PATH',
                'MAGIC_CONVERTER_PHASE_TRACE', 'MAGIC_CONVERTER_EXCEL_VALIDATION_PRINTER',
                'QT_QPA_PLATFORM_PLUGIN_PATH'):
        env.pop(key, None)
    env.update(PYTHONUTF8='1', QT_QPA_PLATFORM='offscreen',
               MAGIC_CONVERTER_TEST_OFFICE='0')

    def save():
        tmp = out / 'result.tmp'
        tmp.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
        tmp.replace(out / 'result.json')

    def run(name, command, timeout=1800, *, clean_path=False):
        command = list(map(str, command))
        stage = {'status': 'running', 'command': command}
        with gate:
            result['stages'][name] = stage; save()
        profiles = out / 'profiles' / archive.name if a.resume else out / 'profiles'
        child_env = {**env, 'MAGIC_CONVERTER_PROFILE_DIR': str(profiles / name)}
        if clean_path:
            child_env['PATH'] = str(Path(os.environ.get('SystemRoot', 'C:/Windows')) / 'System32')
        started = time.monotonic()
        try:
            with (out / (name + '.log')).open('wb') as log:
                process = subprocess.run(command, cwd=ROOT, env=child_env, stdout=log, stderr=subprocess.STDOUT,
                                         timeout=timeout, creationflags=subprocess.CREATE_NO_WINDOW)
            stage['exit'] = process.returncode
            if process.returncode:
                raise RuntimeError(f'{name} exited {process.returncode}')
            stage['status'] = 'passed'
        except Exception as error:
            stage.update(status='failed', error=f'{type(error).__name__}: {error}')
            raise
        finally:
            stage['seconds'] = round(time.monotonic() - started, 3)
            with gate:
                save()
                print(f"{name}: {stage['status']}", flush=True)

    def verify(label, tool, *args, timeout=1800):
        run(label, [*python, ROOT / 'tools/validation' / tool, *args], timeout)

    def frozen_checks():
        for name, executable in (('gui', gui), ('cli', cli)):
            report = out / (name + '-self-test.json')
            run(name + '-self-test', [executable, '--self-test', report], 240, clean_path=True)
            self_test = json.loads(report.read_text(encoding='utf-8'))
            assert self_test['status'] == 'passed' and self_test['frozen'] is True
        verify('cli-integration', 'verify_cli.py', cli, '--report', out / 'cli-integration.json')

    def workbook(label, executable=None):
        args = ['--source', a.large_workbook.resolve(), '--output', out / label, '--timeout', '3600']
        if executable is not None:
            args.extend(['--executable', executable])
        verify(label, 'verify_large_workbook.py', *args, timeout=5400)

    def validate_resume():
        expected = {'build', 'full-suite', 'reproduction', 'large-source', 'large-frozen',
                    'gui-self-test', 'cli-self-test', 'cli-integration'}
        stages = prior['stages']
        if set(stages) != expected or stages['full-suite']['status'] != 'failed' or any(
                stage['status'] != 'passed' for name, stage in stages.items() if name != 'full-suite'):
            raise ValueError('Resume only accepts an isolated suite failure with all other prerequisite stages passed')
        changed = set(git('diff', '--name-only', prior['source_commit'], 'HEAD').splitlines())
        if changed - {'tools/validation/run_internal_release.py'}:
            raise ValueError('Product/test inputs changed; rebuild and validate a new run')
        if sha(a.baseline) != prior['baseline_sha256'] or sha(policy) != prior['policy_sha256']:
            raise ValueError('Baseline or release policy changed')
        repro = json.loads((out / 'reproduction/result.json').read_text(encoding='utf-8'))
        artifacts = {'magic_converter.exe', 'magic_converter-cli.exe', 'sbom.spdx.json', 'build-inputs.json'}
        if (repro['status'] != 'passed' or repro['source_self_test_exit'] != 0
                or repro['work_path'] != a.work_path.as_posix() or len(repro['compared']) != 4
                or {item['file'] for item in repro['compared']} != artifacts
                or any(not item['identical'] or sha(release / item['file']) != item['original'] for item in repro['compared'])):
            raise ValueError('Previous reproduction does not attest the current built artifacts')
        inputs = json.loads((release / 'build-inputs.json').read_text(encoding='utf-8'))
        if any(sha(ROOT / item['source']) != item['sha256'] for item in inputs['files'] if item['owner'] == 'project'):
            raise ValueError('Packaged project inputs changed')
        source_sha = sha(a.large_workbook)
        for label in ('large-source', 'large-frozen'):
            audit = json.loads((out / label / 'report.json').read_text(encoding='utf-8'))
            if audit['status'] != 'passed' or not audit['original_unchanged'] or audit['source_sha256'] != source_sha:
                raise ValueError('Previous workbook evidence has a different input or outcome')
            if label == 'large-frozen' and audit['executable_sha256'] != sha(cli):
                raise ValueError('Previous workbook evidence used a different CLI executable')
        result['reused_stages'] = sorted(expected - {'full-suite'})

    save()
    try:
        if git('status', '--porcelain'):
            raise ValueError('Validate a clean, committed source tree')
        current_commit = git('rev-parse', 'HEAD')
        result['verification_commit'] = current_commit
        if not a.baseline.is_file() or not a.large_workbook.is_file():
            raise ValueError('Required baseline executable or workbook is missing')
        if a.phase_baseline is not None and not a.phase_baseline.is_file():
            raise ValueError('Instrumented benchmark baseline is missing')
        if a.historical_workbook is not None:
            history = result['historical_workbook'] = {}
            original_sha = sha(a.large_workbook)
            for name, phased, pairs in (('history-phases', True, 3), ('history-released', False, 2)):
                path = a.historical_workbook.resolve() / name / 'report.json'
                raw = path.read_bytes()
                comparison = json.loads(raw)
                if (comparison['status'] != 'passed' or comparison['source_sha256'] != original_sha
                        or comparison['policy']['phase_trace'] != phased
                        or comparison['policy']['measurements_per_executable'] != pairs
                        or not comparison['original_unchanged'] or len(comparison['runs']) != 2 + 2 * pairs
                        or any(run['status'] != 'passed' for run in comparison['runs'])):
                    raise ValueError('Historical report does not attest the complete workbook')
                history[name] = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
        if a.workbook_benchmark is not None:
            path = a.workbook_benchmark.resolve()
            raw = path.read_bytes()
            benchmark = json.loads(raw)
            if (benchmark['status'] != 'passed' or not benchmark['original_unchanged']
                    or benchmark['source_sha256'] != sha(a.large_workbook)
                    or benchmark['executables']['candidate']['sha256'] != sha(a.baseline)
                    or len(benchmark['runs']) != 8 or any(run['status'] != 'passed' for run in benchmark['runs'])):
                raise ValueError('The benchmark must attest the same prior CLI and complete workbook')
            result['workbook_benchmark'] = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
        if a.workbook_host is not None:
            path = a.workbook_host.resolve()
            raw = path.read_bytes()
            host = json.loads(raw)
            if (host['status'] != 'passed' or not host['original_unchanged']
                    or host['source_sha256'] != sha(a.large_workbook)
                    or host['executables']['published-2.2.25']['sha256'] != sha(a.baseline)
                    or len(host['runs']) != 9 or any(run['status'] != 'passed' for run in host['runs'])):
                raise ValueError('The host comparison must attest the prior CLI and the same complete workbook')
            result['workbook_host'] = {'path': str(path), 'sha256': hashlib.sha256(raw).hexdigest()}
        result['environment_preflight'] = check_environment(); save()
        if a.resume:
            validate_resume()
        else:
            result['source_commit'] = current_commit
            result['baseline_sha256'] = sha(a.baseline)
            result['policy_sha256'] = sha(policy)
            run('build', ['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', ROOT / 'build.ps1',
                          '-Offline', '-SkipInstall', '-WorkPath', a.work_path.as_posix()])
            if a.phase_baseline is not None:
                verify('workbook-phases', 'compare_workbook_performance.py', '--phases',
                       '--source', a.large_workbook.resolve(), '--baseline', a.phase_baseline.resolve(),
                       '--candidate', cli, '--pairs', str(a.phase_pairs), '--output', out / 'workbook-phases', timeout=9000)
        # The suite's real CLI/IPC and Office deadlines are assessed without
        # concurrent build installations or large workbook scans.
        run('full-suite', [*python, '-m', 'unittest', 'discover', '-s', 'tests', '-v'])
        if not a.resume:
            lanes = [lambda: verify('reproduction', 'verify_repro_build.py', '--output', out / 'reproduction',
                                    '--work-path', a.work_path.as_posix()),
                     lambda: workbook('large-source'), lambda: workbook('large-frozen', cli), frozen_checks]
            with ThreadPoolExecutor(max_workers=4) as workers:
                for future in [workers.submit(lane) for lane in lanes]:
                    future.result()
        verify('word-inherited', 'verify_word_inherited_metrics.py', '--output', out / 'word-inherited',
               '--executable', cli, '--office')
        verify('masked-text', 'verify_masked_text.py', '--output', out / 'masked-text',
               '--executable', cli, '--office')
        verify('editable-recovery', 'verify_editable_recovery.py', '--output', out / 'editable-recovery',
               '--executable', cli, '--baseline', a.baseline.resolve(), '--suite', 'v2224', '--office', '--regression')
        verify('pdf-corner', 'verify_editable_recovery.py', '--output', out / 'pdf-corner',
               '--executable', cli, '--baseline', a.baseline.resolve(), '--suite', 'v2228', '--kind', 'pdf', '--office')
        verify('word-hwpx', 'verify_word_hwpx_v2228.py', '--output', out / 'word-hwpx',
               '--executable', cli, '--baseline', a.baseline.resolve())
        if a.excel_print_fixtures is not None:
            tool = 'verify_excel_print_v2228.py'
            print_args = ['--source', a.large_workbook.resolve(), '--fixtures', a.excel_print_fixtures.resolve(),
                          '--executable', cli, '--output', out / 'excel-print']
            if a.excel_print_printers is not None:
                tool = 'verify_excel_print_expanded.py'
                print_args.extend(['--printers', a.excel_print_printers.resolve()])
            verify('excel-print', tool, *print_args)
        run('native-review', [*python, ROOT / 'tools/build/review_native_sources.py', '--release', release,
            '--source-repo', ROOT / '.build-cache/upstream/rhwp-v0.8.7.git', '--expected-commit', '1a76570e833917d15817415a53c09ad61ab3203f',
            '--workspace-source', ROOT / 'dist/v2.2.18/validation/source-materials/rhwp-workspace-source-review.zip',
            '--crate-cache', ROOT / '.build-cache/sources/cargo', '--source-cache', ROOT / '.build-cache/sources',
            '--native-build-receipt', ROOT / 'dist/v2.2.18/validation/stage/native-compile.json',
            '--native-build-archive', ROOT / 'dist/v2.2.18/validation/source-materials/rhwp-rebuilt-review.zip',
            '--output', out / 'native-review'])
        audit = [json.loads((out / label / 'report.json').read_text(encoding='utf-8'))
                 for label in ('large-source', 'large-frozen')]
        assert all(item['status'] == 'passed' and item['original_unchanged'] for item in audit)
        assert audit[0]['actual']['content_sha256'] == audit[1]['actual']['content_sha256']
        reproduction = json.loads((out / 'reproduction/result.json').read_text(encoding='utf-8'))
        assert reproduction['status'] == 'passed' and reproduction['source_self_test_exit'] == 0
        assert len(reproduction['compared']) == 4 and all(item['identical'] for item in reproduction['compared'])
        assert git('rev-parse', 'HEAD') == current_commit and not git('status', '--porcelain')
        assert sha(policy) == result['policy_sha256']
        result['executable_sha256'] = {'gui': sha(gui), 'cli': sha(cli)}
        result['status'] = 'passed'
    except Exception as error:
        result.update(status='failed', error=f'{type(error).__name__}: {error}')
    result['finished_utc'] = datetime.now(timezone.utc).isoformat(); save()
    print(result['status'], flush=True)
    return int(result['status'] != 'passed')


if __name__ == '__main__': raise SystemExit(main())
