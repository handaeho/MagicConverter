"""Measure PDF save phases with Windows counters that do not require ETW rights."""
import argparse
import ctypes
import json
import math
import os
from pathlib import Path
import platform
from statistics import median
import subprocess
import sys
from threading import Event, Thread
from time import perf_counter_ns, sleep


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.compare_workbook_performance import measure_conversion
from tools.validation.verify_large_workbook import pdf_contents, sha, source_contents


COUNTERS = {
    'cpu_percent': r'\Processor(_Total)\% Processor Time',
    'cpu_relative_performance_percent': r'\Processor Information(_Total)\% Processor Performance',
    'ready_queue': r'\System\Processor Queue Length',
    'disk_read_bytes_per_second': r'\LogicalDisk({volume})\Disk Read Bytes/sec',
    'disk_write_bytes_per_second': r'\LogicalDisk({volume})\Disk Write Bytes/sec',
    'disk_read_seconds': r'\LogicalDisk({volume})\Avg. Disk sec/Read',
    'disk_write_seconds': r'\LogicalDisk({volume})\Avg. Disk sec/Write',
    'disk_queue': r'\LogicalDisk({volume})\Avg. Disk Queue Length',
    'physical_disk_queue': r'\PhysicalDisk(_Total)\Avg. Disk Queue Length',
}
STAGES = ('font_subset', 'chunk_save_close', 'open_pdfs', 'insert_pdf', 'incremental_save_close', 'first_copy')
ORDER = (('published-2.2.25', False), ('2.2.25', False), ('2.2.25', True), ('2.2.21', True),
         ('2.2.22', True), ('2.2.22', True), ('2.2.21', True), ('2.2.25', True), ('2.2.25', False))


class CounterValue(ctypes.Structure):
    _fields_ = [('status', ctypes.c_uint32), ('value', ctypes.c_double)]


class HostCounters:
    def __init__(self, volume):
        self.dll = ctypes.WinDLL('pdh')
        self.dll.PdhOpenQueryW.argtypes = [ctypes.c_wchar_p, ctypes.c_size_t, ctypes.POINTER(ctypes.c_void_p)]
        self.dll.PdhAddEnglishCounterW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_size_t,
                                                  ctypes.POINTER(ctypes.c_void_p)]
        self.dll.PdhGetFormattedCounterValue.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
                                                        ctypes.POINTER(CounterValue)]
        for name in ('PdhCollectQueryData', 'PdhCloseQuery'):
            getattr(self.dll, name).argtypes = [ctypes.c_void_p]
        self.query = ctypes.c_void_p()
        self.done = Event()
        self.thread = Thread(target=self._sample, daemon=True)
        self.samples = []
        self.status = {}
        self.handles = {}
        self.error = None
        self.volume = volume

    def __enter__(self):
        status = self.dll.PdhOpenQueryW(None, 0, ctypes.byref(self.query))
        if status:
            raise OSError(f'PDH open error 0x{status & 0xffffffff:08x}')
        for label, path in COUNTERS.items():
            handle = ctypes.c_void_p()
            status = self.dll.PdhAddEnglishCounterW(self.query, path.format(volume=self.volume), 0, ctypes.byref(handle))
            self.status[label] = f'0x{status & 0xffffffff:08x}'
            if not status:
                self.handles[label] = handle
        self.thread.start()
        return self

    def __exit__(self, exc_type, exc, traceback):
        self.done.set()
        self.thread.join()
        self.dll.PdhCloseQuery(self.query)

    def _sample(self):
        start = perf_counter_ns()
        while True:
            status = self.dll.PdhCollectQueryData(self.query)
            end = perf_counter_ns()
            if status:
                self.error = f'PDH collect error 0x{status & 0xffffffff:08x}'
            else:
                sample = {'start_ns': start, 'end_ns': end}
                for label, handle in self.handles.items():
                    value = CounterValue()
                    if (not self.dll.PdhGetFormattedCounterValue(handle, 0x8200, None, ctypes.byref(value))
                            and value.status in (0, 1) and math.isfinite(value.value)):
                        sample[label] = value.value
                self.samples.append(sample)
            if self.done.wait(1):
                return
            start = end

    def receipt(self):
        return {'volume': self.volume, 'add_status': self.status, 'last_error': self.error,
                'samples': self.samples}


def write(path, data):
    scratch = path.with_suffix('.tmp')
    scratch.write_text(json.dumps(data, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    scratch.replace(path)


def summarize_chunks(conversion, expected):
    chunks = [event for event in conversion['trace'] if event['phase'] == 'workbook_chunk']
    if len(chunks) != expected or [chunk['chunk'] for chunk in chunks] != list(range(1, expected + 1)):
        raise ValueError('Missing or misordered detailed save events')
    metrics = {stage: {'wall_ns': 0, 'thread_cpu_ns': 0} for stage in STAGES}
    intervals = []
    pages = 0
    for chunk in chunks:
        if chunk['merged_before'] != pages:
            raise ValueError('Page count at save boundary differs')
        keys = STAGES[:2] + (STAGES[2:5] if pages else STAGES[5:])
        clocks = chunk['clocks']
        if len(clocks) != len(keys) + 1:
            raise ValueError('Unexpected save stage boundaries')
        for stage, (start, cpu_start), (end, cpu_end) in zip(keys, clocks, clocks[1:]):
            if end <= start or cpu_end < cpu_start:
                raise ValueError('Unexpected clock order')
            metrics[stage]['wall_ns'] += end - start
            metrics[stage]['thread_cpu_ns'] += cpu_end - cpu_start
            intervals.append((start, end))
        pages += chunk['pages']
    if pages != 29_448:
        raise ValueError('Unexpected page total in detailed save events')
    return metrics, intervals


def summarize_counters(samples, save_intervals):
    values = {label: [] for label in COUNTERS}
    for sample in samples:
        if any(start < sample['end_ns'] and sample['start_ns'] < end for start, end in save_intervals):
            for label in values.keys() & sample.keys():
                values[label].append(sample[label])
    return {label: {'samples': len(data), 'median': median(data), 'max': max(data),
                    'p95': sorted(data)[math.ceil(.95 * len(data)) - 1]}
            for label, data in values.items() if data}


def benchmark(source, builds, output, volume):
    receipt = output / 'report.json'
    frozen = ROOT / 'dist/v2.2.25/magic_converter-cli.exe'
    base = json.loads((frozen.parent / 'validation/summary.json').read_text(encoding='utf-8'))
    expected = base['workbooks']['large-frozen']
    origins = json.loads((builds / 'sources.json').read_text(encoding='utf-8'))
    executables = {'published-2.2.25': frozen}
    for version, origin in origins.items():
        release = builds / ('v' + version) / 'dist' / ('v' + version)
        verification = json.loads((release / 'release-verification.json').read_text(encoding='utf-8'))
        executable = release / frozen.name
        assert verification['status'] == 'passed'
        assert sha(executable) != origin['published_sha256']
        executables[version] = executable
    power = subprocess.check_output(['powercfg', '/getactivescheme'], creationflags=subprocess.CREATE_NO_WINDOW)
    report = {'status': 'running', 'source': str(source), 'source_sha256': sha(source),
              'source_snapshots': origins, 'windows': platform.version(), 'logical_cpus': os.cpu_count(),
              'power_scheme': power.decode('oem', errors='replace').strip(),
              'executables': {key: {'path': str(path), 'sha256': sha(path)} for key, path in executables.items()},
              'policy': {'host_mode': '1 second PDH sampling with ETW unavailable',
                         'application_mode': 'monotonic clocks and worker thread CPU between PDF calls',
                         'scope': 'host counters are aggregate and are not process-attributed I/O or scheduler traces',
                         'limits': 'Windows thread CPU accounting may be quantized. Wall time minus thread CPU is not a disk wait measurement.',
                         'order': ORDER}, 'runs': []}
    write(receipt, report)
    try:
        assert report['executables']['published-2.2.25']['sha256'] == origins['2.2.25']['published_sha256']
        assert report['source_sha256'] == expected['source_sha256'] and source_contents(source) == expected['expected']
        for index, (version, tracked) in enumerate(ORDER, 1):
            folder = output / f'{index:02d}-{version}'
            folder.mkdir()
            item = {'version': version, 'host_counters_enabled': tracked, 'status': 'running'}
            report['runs'].append(item)
            write(receipt, report)
            if tracked:
                with HostCounters(volume) as host:
                    conversion = measure_conversion(executables[version], source, folder, phases=True)
                host_receipt = host.receipt()
                write(folder / 'host-counters.json', host_receipt)
                item['host_counter_status'] = {key: value for key, value in host_receipt.items() if key != 'samples'}
            else:
                conversion = measure_conversion(executables[version], source, folder, phases=True)
            item['conversion'] = conversion
            if version != 'published-2.2.25':
                item['save_stages'], intervals = summarize_chunks(conversion, 58 if version == '2.2.25' else 231)
                if tracked:
                    item['host_during_save'] = summarize_counters(host_receipt['samples'], intervals)
                    item['host_counters_sha256'] = sha(folder / 'host-counters.json')
            item['status'] = 'converted'
            write(receipt, report)
            print(f'{index:02d} {version}: {conversion["seconds"]:.3f}s', flush=True)
        for index, item in enumerate(report['runs'], 1):
            folder = output / f'{index:02d}-{item["version"]}'
            output_pdf, = item['conversion']['outputs']
            assert output_pdf['bytes'] <= 256 * 1024**2
            item['actual'] = pdf_contents(Path(output_pdf['path']), folder)
            assert item['actual'] == expected['actual']
            item['status'] = 'passed'
            write(receipt, report)
            print(f'{index:02d} content verified', flush=True)
        assert sha(source) == report['source_sha256']
        assert all(sha(path) == report['executables'][key]['sha256'] for key, path in executables.items())
        report.update(status='passed', original_unchanged=True)
    except Exception as error:
        report.update(status='failed', error=f'{type(error).__name__}: {error}')
    write(receipt, report)
    print(report['status'], report.get('error', ''), flush=True)
    return int(report['status'] != 'passed')


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--source', type=Path)
    parser.add_argument('--builds', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--probe', action='store_true')
    args = parser.parse_args()
    out = args.output.resolve()
    if not out.is_relative_to((ROOT / '.test-artifacts').resolve()) or sys.flags.optimize:
        parser.error('Use an output below .test-artifacts and keep assertions enabled')
    out.mkdir(parents=True, exist_ok=False)
    if args.probe:
        with HostCounters(out.drive) as host:
            sleep(3)
        probe = host.receipt()
        write(out / 'report.json', probe)
        print(probe['add_status'], probe['last_error'], flush=True)
        return 0
    if args.source is None or args.builds is None:
        parser.error('Full comparison requires the original workbook and diagnostic builds')
    return benchmark(args.source.resolve(), args.builds.resolve(), out, out.drive)


if __name__ == '__main__':
    raise SystemExit(main())
