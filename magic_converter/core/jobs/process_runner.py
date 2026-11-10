"""Cancellable child processes with deadlines and no inherited output pipes."""

from __future__ import annotations

import codecs
import ctypes
import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from magic_converter.core.jobs.job_control import JobTimedOut, check_cancelled, current


@dataclass(frozen=True)
class ProcessResult:
    returncode: int
    stdout: str
    stderr: str


class _ProcessDirectory(tempfile.TemporaryDirectory):
    def cleanup(self) -> None:
        # Windows can briefly keep a terminated process's file handles alive
        # after its wait handle is signalled. Retry only this owned directory.
        for attempt in range(100):
            try:
                return super().cleanup()
            except PermissionError:
                if attempt == 99:
                    raise
                time.sleep(0.02)


def create_job(process, memory_limit=None):
    if os.name != "nt":
        return None
    import win32job
    job = None
    try:
        job = win32job.CreateJobObject(None, "")
        info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
        info["BasicLimitInformation"]["LimitFlags"] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if memory_limit is not None:
            info["BasicLimitInformation"]["LimitFlags"] |= (
                win32job.JOB_OBJECT_LIMIT_PROCESS_MEMORY | win32job.JOB_OBJECT_LIMIT_JOB_MEMORY
            )
            info["ProcessMemoryLimit"] = info["JobMemoryLimit"] = memory_limit
        win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
        win32job.AssignProcessToJobObject(job, process._handle)
        return job
    except Exception:
        if job is not None:
            job.Close()
        return None


def start_process(command: Sequence[str], *, stdout, stderr, cwd=None, memory_limit=None, hidden=False):
    # A fast parent can spawn a child and exit before AssignProcessToJobObject.
    # Start suspended, establish ownership, then resume the process on Windows.
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if os.name == "nt":
        import win32job  # Load the binding before the child exists.
        resume = ctypes.WinDLL("ntdll").NtResumeProcess
        resume.argtypes = [ctypes.c_void_p]
        resume.restype = ctypes.c_long
        flags |= 0x00000004  # CREATE_SUSPENDED (not exported by subprocess).
    options = {}
    if os.name == 'nt' and hidden:
        startup = subprocess.STARTUPINFO()
        startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        startup.wShowWindow = 0
        options['startupinfo'] = startup
    process = subprocess.Popen(list(command), stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                               cwd=cwd, creationflags=flags, start_new_session=os.name != "nt", **options)
    job = None
    try:
        job = create_job(process, memory_limit)
        check_cancelled()
        if os.name == "nt":
            if job is None:
                raise OSError("변환 프로세스를 Windows 작업에 격리할 수 없습니다.")
            status = resume(int(process._handle))
            if status < 0:
                raise OSError(f"변환 프로세스를 시작하지 못했습니다 (NTSTATUS {status & 0xffffffff:08x}).")
        return process, job
    except BaseException:
        stop_process(process, job)
        raise


def stop_process(process, job) -> None:
    if job is not None:
        import win32job
        try:
            # Closing a kill-on-close job initiates termination asynchronously.
            # Wait for all owned children before deleting their inherited logs.
            win32job.TerminateJobObject(job, 1)
            until = time.monotonic() + 3
            while win32job.QueryInformationJobObject(job, win32job.JobObjectBasicAccountingInformation)["ActiveProcesses"]:
                if time.monotonic() >= until:
                    break
                time.sleep(0.01)
        finally:
            job.Close()
    elif os.name != "nt":
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.poll() is None:
        # The PID still belongs to our live Popen object. Restrict fallback
        # termination to its tree, never to an executable's name.
        try:
            subprocess.run([str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32/taskkill.exe"),
                            "/PID", str(process.pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=3,
                           creationflags=subprocess.CREATE_NO_WINDOW, check=False)
        except (OSError, subprocess.TimeoutExpired):
            pass
    if process.poll() is None:
        process.kill()
    try:
        process.wait(timeout=3)
    except subprocess.TimeoutExpired:
        pass


def run_process(command: Sequence[str], *, timeout: float | None = None,
                idle_timeout: float | None = None, on_output: Callable[[str], None] | None = None,
                cwd: str | Path | None = None, capture_limit: int = 2 * 1024 * 1024,
                log_limit: int = 64 * 1024 * 1024, memory_limit: int | None = None,
                on_poll: Callable[[], None] | None = None) -> ProcessResult:
    if (not command or timeout is not None and timeout <= 0 or idle_timeout is not None and idle_timeout <= 0
            or capture_limit <= 0 or log_limit <= 0 or memory_limit is not None and memory_limit <= 0):
        raise ValueError("A command and positive timeouts are required")
    check_cancelled()
    started = activity = time.monotonic()
    with _ProcessDirectory(prefix="magic-process-") as directory:
        root = Path(directory)
        stdout_path, stderr_path = root / "stdout", root / "stderr"
        with stdout_path.open("wb", buffering=0) as output, stderr_path.open("wb", buffering=0) as errors:
            process, job = start_process(command, stdout=output, stderr=errors, cwd=cwd, memory_limit=memory_limit)
            stopped = False
            try:
                decoder = codecs.getincrementaldecoder("utf-8")("replace")
                pending, error_size = "", 0
                with stdout_path.open("rb") as incoming:
                    def drain() -> bool:
                        nonlocal pending
                        received = False
                        for _ in range(8):
                            chunk = incoming.read(65536)
                            if not chunk:
                                break
                            received = True
                            if on_output is not None:
                                pending += decoder.decode(chunk)
                                lines = pending.split("\n")
                                pending = lines.pop()[-65536:]
                                for line in lines:
                                    on_output(line.rstrip("\r"))
                        return received

                    while True:
                        check_cancelled()
                        if on_poll is not None:
                            on_poll()
                        changed = drain()
                        size = stderr_path.stat().st_size
                        if size + stdout_path.stat().st_size > log_limit:
                            raise RuntimeError("변환 엔진의 로그 크기가 제한을 초과했습니다.")
                        if changed or size != error_size:
                            activity, error_size = time.monotonic(), size
                        code = process.poll()
                        if code is not None:
                            # Stop inheriting children before draining the final
                            # bytes; they must not keep writing after their parent exits.
                            stopped = True
                            stop_process(process, job)
                            while drain():
                                check_cancelled()
                            if on_output is not None:
                                pending += decoder.decode(b"", final=True)
                                if pending:
                                    on_output(pending.rstrip("\r"))
                            check_cancelled()
                            break
                        now = time.monotonic()
                        label = Path(command[0]).name
                        if timeout is not None and now - started >= timeout:
                            raise JobTimedOut(f"{label} 실행 제한 시간({timeout:g}초)이 초과되었습니다.")
                        if idle_timeout is not None and now - activity >= idle_timeout:
                            raise JobTimedOut(f"{label}에서 {idle_timeout:g}초 동안 응답이 없습니다.")
                        control = current.get()
                        if control is None:
                            time.sleep(0.05)
                        else:
                            control.event.wait(0.05)
            finally:
                if not stopped:
                    stop_process(process, job)

        def tail(path: Path) -> str:
            with path.open("rb") as stream:
                stream.seek(max(0, path.stat().st_size - capture_limit))
                return stream.read(capture_limit).decode("utf-8", "replace")

        return ProcessResult(code, tail(stdout_path), tail(stderr_path))
