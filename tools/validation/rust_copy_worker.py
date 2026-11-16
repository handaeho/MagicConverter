"""Development-only adapter for the Rust copy pilot."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter_ns

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.jobs.process_runner import run_process


def copy_with_worker(worker: Path, source: Path, temporary: Path, progress=lambda _: None, *,
                     wake_on_exit: bool = True) -> dict[str, int]:
    """Write into the Python publisher's owned temporary file by a same-volume rename."""
    check_cancelled()
    expected = source.stat().st_size
    with TemporaryDirectory(prefix=".magic-copy-", dir=temporary.parent) as directory:
        staged = Path(directory) / "payload"
        started = perf_counter_ns()
        launched = None

        def poll() -> None:
            nonlocal launched
            if launched is None:
                launched = perf_counter_ns()

        def update(line: str) -> None:
            kind, _, value = line.partition(" ")
            if kind == "progress" and 1 <= len(value) <= 2 and value.isascii() and value.isdecimal():
                progress(int(value))

        result = run_process([str(worker), str(source), str(staged)], on_output=update, on_poll=poll,
                             wake_on_exit=wake_on_exit)
        completed = perf_counter_ns()
        if result.returncode:
            raise ConversionError(f"Rust 복사 작업자가 종료되었습니다 ({result.returncode}): {result.stderr.strip()}")
        lines = result.stdout.splitlines()
        fields = lines[-1].split() if lines else []
        check_cancelled()
        if (len(fields) != 3 or fields[:2] != ["copied", str(expected)]
                or len(fields[2]) > 24 or not fields[2].isascii() or not fields[2].isdecimal()
                or not staged.is_file() or staged.stat().st_size != expected):
            raise ConversionError("Rust 복사 작업자의 출력 크기 또는 완료 응답이 올바르지 않습니다.")
        os.replace(staged, temporary)
        return {"stream_ns": int(fields[2]), "supervised_ns": completed - started,
                "launch_ns": launched - started, "validate_ns": perf_counter_ns() - completed,
                "active_start_ns": started, "active_end_ns": completed, "payload_bytes": expected}
