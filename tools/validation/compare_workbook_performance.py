"""Serial, alternating frozen CLI benchmark with separate content audits."""
import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import re
import statistics
import subprocess
import sys
import time
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter.core.diagnostics.visual_comparison import sha
from tools.validation.verify_large_input_rss import measure
from tools.validation.verify_large_workbook import pdf_contents, source_contents


def cpu_times():
    idle, kernel, user = (ctypes.c_uint64() for _ in range(3))
    if not ctypes.windll.kernel32.GetSystemTimes(*(ctypes.byref(value) for value in (idle, kernel, user))):
        raise ctypes.WinError()
    return idle.value, kernel.value + user.value


def busy_fraction(before, after):
    return round(1 - (after[0] - before[0]) / (after[1] - before[1]), 4)


def measure_conversion(executable, source, folder, *, phases):
    command = [str(executable), 'convert', str(source), '--action', 'document:pdf',
               '--output', str(folder / 'output'), '--data-dir', str(folder / 'profile'),
               '--timeout', '3600', '--json']
    if not phases:
        return measure(command, folder / 'measurement', expected_ok=True, deadline=3660)
    trace = folder / 'trace'
    trace.mkdir()
    with patch.dict(os.environ, MAGIC_CONVERTER_PHASE_TRACE=str(trace)):
        result = measure(command, folder / 'measurement', expected_ok=True, deadline=3660)
    events = [json.loads(line) for path in trace.glob('*.jsonl')
              for line in path.read_text(encoding='ascii').splitlines()]
    milestones = {}
    for name in ('conversion_start', 'workbook_start', 'workbook_done', 'conversion_complete'):
        matches = [event for event in events if event['phase'] == name]
        if len(matches) != 1:
            raise ValueError(f'Expected one {name} timestamp, got {len(matches)}')
        milestones[name] = matches[0]
    clock = result['clock_ns']
    ticks = [clock['launch'], *(item['ns'] for item in milestones.values()),
             clock['root_exit_observed'], clock['tree_exit_observed']]
    if ticks != sorted(ticks):
        raise ValueError('Unexpected process and workbook timestamp order')
    entry = [event['ns'] for event in events if event['phase'] == 'python_entry'
             and event['pid'] == milestones['conversion_start']['pid']]
    if len(entry) != 1 or not ticks[0] <= entry[0] <= ticks[1]:
        raise ValueError('Missing primary Python entry timestamp')
    result['phase_seconds'] = {
        'startup': (ticks[1] - ticks[0]) / 1e9,
        'bootloader_to_python': (entry[0] - ticks[0]) / 1e9,
        'conversion': (ticks[4] - ticks[1]) / 1e9,
        'before_workbook': (ticks[2] - ticks[1]) / 1e9,
        'workbook': (ticks[3] - ticks[2]) / 1e9,
        'publication_and_completion': (ticks[4] - ticks[3]) / 1e9,
        'shutdown': (ticks[6] - ticks[4]) / 1e9,
    }
    result['workbook_seconds'] = {key.removesuffix('_ns'): value / 1e9
                                  for key, value in milestones['workbook_done'].items() if key.endswith('_ns')}
    result['trace'] = sorted(events, key=lambda event: event['ns'])
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', required=True, type=Path)
    parser.add_argument('--baseline', required=True, type=Path)
    parser.add_argument('--candidate', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--phases', action='store_true')
    parser.add_argument('--pairs', type=int, choices=(2, 3), default=3)
    args = parser.parse_args()
    out, source = args.output.resolve(), args.source.resolve()
    if not out.is_relative_to((ROOT / '.test-artifacts').resolve()):
        parser.error('Output must stay under project .test-artifacts')
    if sys.flags.optimize:
        parser.error('Run validation without Python optimization')
    out.mkdir(parents=True, exist_ok=False)
    executables = {'baseline': args.baseline.resolve(), 'candidate': args.candidate.resolve()}
    power = subprocess.run(['powercfg', '/getactivescheme'], capture_output=True,
                           timeout=10, creationflags=subprocess.CREATE_NO_WINDOW)
    guid = re.search(rb'[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}', power.stdout)
    report = {'status': 'running', 'source': str(source), 'source_sha256': sha(source),
              'executables': {key: {'path': str(exe), 'sha256': sha(exe)} for key, exe in executables.items()},
              'environment': {'windows': platform.version(), 'architecture': platform.machine(),
                              'logical_cpus': os.cpu_count(), 'power_scheme_guid': guid[0].decode('ascii') if guid else None,
                              'power_scheme_exit': power.returncode},
              'policy': {'warmups_per_executable': 1, 'measurements_per_executable': args.pairs,
                         'timeout_seconds': 3600, 'rss_sample_interval_seconds': .025,
                         'scope': 'CLI process wall time, including startup and shutdown; full content audits afterward',
                         'cpu_scope': 'Total host CPU activity before and during each process; during includes conversion',
                         'phase_trace': args.phases},
              'runs': []}

    def save():
        pending = out / 'report.tmp'
        pending.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
        pending.replace(out / 'report.json')

    save()
    try:
        report['expected'] = source_contents(source)
        save()
        baseline, candidate = executables
        pair_order = [(baseline, candidate) if index % 2 == 0 else (candidate, baseline)
                      for index in range(args.pairs)]
        order = [baseline, candidate, *(key for pair in pair_order for key in pair)]
        for index, key in enumerate(order):
            label = f'{index + 1:02}-{key}'
            folder = out / label
            folder.mkdir()
            run = {'label': label, 'executable': key, 'warmup': index < 2, 'status': 'running'}
            report['runs'].append(run)
            save()
            before = cpu_times()
            time.sleep(2)
            started = cpu_times()
            run['pre_run_cpu_busy_fraction'] = busy_fraction(before, started)
            run['conversion'] = measure_conversion(executables[key], source, folder, phases=args.phases)
            run['run_cpu_busy_fraction'] = busy_fraction(started, cpu_times())
            assert len(run['conversion']['outputs']) == 1
            run['status'] = 'converted'
            save()
            print(f"{label}: {run['conversion']['seconds']:.3f}s", flush=True)

        for run in report['runs']:
            folder = out / run['label']
            pdf = Path(run['conversion']['outputs'][0]['path'])
            assert pdf.stat().st_size <= 256 * 1024**2
            run['actual'] = pdf_contents(pdf, folder)
            assert all(run['actual'][key] == report['expected'][key] for key in ('cells', 'content_sha256'))
            run['status'] = 'passed'
            save()
            print(f"{run['label']}: content verified", flush=True)
        assert sha(source) == report['source_sha256']
        assert all(sha(exe) == report['executables'][key]['sha256'] for key, exe in executables.items())
        assert len({run['actual']['pages'] for run in report['runs']}) == 1
        report['original_unchanged'] = True
        summary = report['summary'] = {}
        for key in executables:
            conversions = [run['conversion'] for run in report['runs'] if run['executable'] == key and not run['warmup']]
            seconds = [item['seconds'] for item in conversions]
            rss = [item['sampled_tree_peak_rss_bytes'] for item in conversions]
            summary[key] = {'seconds': seconds, 'median_seconds': statistics.median(seconds),
                            'min_seconds': min(seconds), 'max_seconds': max(seconds),
                            'rss_bytes': rss, 'median_rss_bytes': statistics.median(rss)}
            if args.phases:
                for group in ('phase_seconds', 'workbook_seconds'):
                    summary[key][group] = {field: statistics.median(item[group][field] for item in conversions)
                                           for field in conversions[0][group]}
        deltas = []
        for index in range(2, len(report['runs']), 2):
            pair = {run['executable']: run['conversion']['seconds'] for run in report['runs'][index:index + 2]}
            deltas.append(round(pair[candidate] - pair[baseline], 3))
        summary['candidate_minus_baseline_seconds'] = deltas
        summary['candidate_median_change_percent'] = round(
            100 * (summary[candidate]['median_seconds'] / summary[baseline]['median_seconds'] - 1), 3)
        report['status'] = 'passed'
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
    save()
    print(report['status'], flush=True)
    return int(report['status'] != 'passed')


if __name__ == '__main__':
    raise SystemExit(main())
