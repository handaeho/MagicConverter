"""Owned process boundary for non-cooperative specialized parsers/encoders."""

import hashlib
import importlib
import json
import os
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from magic_converter.core.jobs.job_control import Control, current, check_cancelled, copy_file, report_stage, JobTimedOut
from magic_converter.core.jobs.process_runner import run_process, _ProcessDirectory

ENGINES = frozenset({"data", "font", "ebook", "email", "mesh", "geo"})
WORKER_MEMORY_BYTES = 2 * 1024 * 1024 * 1024
WORKER_OUTPUT_BYTES = 256 * 1024 * 1024
MAX_EVENT_BYTES = 4 * 1024 * 1024
MAX_OUTPUT_ENTRIES = 100000


def output_limit(engine):
    # Preserve the archive parser's existing 4 GiB extraction contract.
    return 4 * 1024 ** 3 if engine in {"archives", "archive_tools"} else WORKER_OUTPUT_BYTES


def output_state(path, *, hashes=True, max_bytes=None):
    """Bound a private file/tree without following links or Windows junctions."""
    import stat
    max_bytes = WORKER_OUTPUT_BYTES if max_bytes is None else max_bytes
    total, entries, stack = 0, [], [path]
    while stack:
        check_cancelled()
        item = stack.pop()
        info = item.lstat()
        if (item.is_symlink() or getattr(info, "st_file_attributes", 0)
                & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)):
            raise ValueError("Worker output contains a link/reparse point")
        name = item.relative_to(path).as_posix()
        if stat.S_ISDIR(info.st_mode):
            entries.append({"path": name, "kind": "directory"})
            with os.scandir(item) as children:
                for child in children:
                    stack.append(Path(child.path))
                    # The root itself is not an archive/output member. Polling
                    # also sees the publisher's temporary container/reservation.
                    if len(stack) + len(entries) - 1 > MAX_OUTPUT_ENTRIES + (4 if not hashes else 0):
                        raise ValueError("Worker output entry budget exceeded")
        elif stat.S_ISREG(info.st_mode):
            total += info.st_size
            if total > max_bytes:
                from magic_converter.core.conversion.converter import ConversionError
                raise ConversionError("파서 출력 용량 제한을 초과했습니다.")
            entry = {"path": name, "kind": "file", "bytes": info.st_size}
            if hashes:
                entry["sha256"] = sha(item)
            entries.append(entry)
        else:
            raise ValueError("Worker output is not a regular file/directory")
    return sorted(entries, key=lambda entry: entry["path"])


def sha(path):
    with path.open("rb") as stream:
        digest = hashlib.sha256()
        while True:
            check_cancelled()
            chunk = stream.read(1024 * 1024)
            if not chunk:
                return digest.hexdigest()
            digest.update(chunk)


def worker_command(request, report):
    command = ([sys.executable, "--specialized-convert"] if getattr(sys, "frozen", False)
               else [sys.executable, "-m", "magic_converter.core.jobs.specialized_worker"])
    return [*command, str(request), str(report)]


def helper_main(request_path, report_path):
    request_path, report_path = Path(request_path).resolve(), Path(report_path).resolve()
    request = json.loads(request_path.read_text(encoding="utf-8"))
    identity = request["run_id"]
    events = (request_path.parent / "events.jsonl").open("w", encoding="utf-8", newline="\n")
    event_bytes = 0

    def emit(kind, message):
        nonlocal event_bytes
        # Windowed frozen apps may have sys.stdout=None even when redirected.
        line = json.dumps({"run_id": identity, "kind": kind, "message": str(message)[:2000]}) + "\n"
        event_bytes += len(line.encode("utf-8"))
        if event_bytes > MAX_EVENT_BYTES:
            raise ValueError("Parser event byte budget exceeded")
        events.write(line)
        events.flush()

    token = current.set(Control(threading.Event(), lambda message: emit("stage", message), request.get("deadline")))
    from magic_converter.core.jobs.raw_api import in_worker
    raw_token = in_worker.set(True)
    try:
        if os.name != "nt":
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY_BYTES, WORKER_MEMORY_BYTES))
        destination = Path(request["destination"]).resolve()
        if destination.parent != request_path.parent or report_path.parent != request_path.parent:
            raise ValueError("Worker output escapes its private directory")
        if request.get("operation") == "raw":
            from magic_converter.core.jobs.raw_api import run_request
            run_request(request, destination, lambda message: emit("notice", message))
        elif request.get("operation") == "combined":
            from magic_converter.core.jobs.combined_worker import run_request
            run_request(request, destination, lambda value: emit("progress", value))
        elif request.get("operation") == "library":
            from magic_converter.core.jobs.library_worker import run_request
            run_request(request, destination, lambda value: emit("progress", value),
                        lambda message: emit("notice", message), lambda identity: emit("office", identity))
        else:
            engine = request["engine"]
            if engine not in ENGINES:
                raise ValueError("Unknown specialized engine")
            module = importlib.import_module("magic_converter.core.specialized." + engine + "_converter")
            args = [Path(request["source"]), destination]
            if engine != "font":
                args.append(request["source_kind"])
            args.extend([request["target_kind"], lambda message: emit("notice", message)])
            module.convert(*args)
        check_cancelled()
        state = output_state(destination, max_bytes=output_limit(request["engine"]))
        if request.get("operation") != "library" and not destination.is_file():
            raise ValueError("Specialized parser must return one file")
        result = {"status": "passed", "run_id": identity, "output": state, "pid": os.getpid()}
        code = 0
    except Exception as exc:
        result = {"status": "failed", "run_id": identity, "error_type": type(exc).__name__,
                  "error": str(exc)[:2000] or type(exc).__name__, "pid": os.getpid()}
        if request.get("operation") == "raw":
            from magic_converter.core.jobs.raw_api import error_details
            result["error_details"] = error_details(exc)
        code = 1
    finally:
        in_worker.reset(raw_token)
        current.reset(token)
        events.close()
    report_path.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")
    return code


@contextmanager
def isolated_result(step, suffix, progress, notice, *, operation="specialized", fields=None):
    from magic_converter.core.jobs.job_control import supervised_api
    with supervised_api():
        with _isolated_result(step, suffix, progress, notice, operation=operation, fields=fields) as result:
            yield result


@contextmanager
def _isolated_result(step, suffix, progress, notice, *, operation="specialized", fields=None):
    from magic_converter.core.conversion.converter import ConversionError
    from magic_converter.core.specialized.book_assets import BookLimitError

    check_cancelled()
    control = current.get()
    with _ProcessDirectory(prefix="magic-parser-") as temporary:
        folder = Path(temporary).resolve()
        request_path, report_path = folder / "request.json", folder / "report.json"
        result_path = folder / ("result" + suffix)
        identity = uuid.uuid4().hex
        events_path = folder / "events.jsonl"
        event_offset, pending = 0, b""
        native_handled = set()
        request = {"run_id": identity, "engine": step.edge.engine_id, "source": str(step.source.resolve()),
                   "source_kind": step.edge.source, "target_kind": step.edge.target,
                   "destination": str(result_path), "deadline": control.deadline if control else None}
        request.update(fields or {})
        request["operation"] = operation
        request_path.write_text(json.dumps(request), encoding="utf-8")

        def output(line):
            try:
                event = json.loads(line)
            except ValueError:
                return
            if not isinstance(event, dict) or event.get("run_id") != identity:
                return
            if event.get("kind") == "notice":
                notice(event["message"])
            elif event.get("kind") == "stage":
                report_stage(event["message"])
            elif event.get("kind") == "progress":
                value = event["message"]
                progress(None if value == "None" else min(100, max(0, int(value))))
            elif event.get("kind") == "office":
                if operation != "library":
                    raise ValueError("Native request outside a library worker")
                from magic_converter.core.jobs.office_broker import handle_request
                handle_request(folder, identity, step.source, event["message"], native_handled)

        def check_output():
            nonlocal event_offset, pending
            for path in (result_path, folder / "output"):
                if path.exists():
                    try:
                        output_state(path, hashes=False, max_bytes=output_limit(step.edge.engine_id))
                    except FileNotFoundError:
                        # Publishers rename private temporary files/directories.
                        # The complete, stopped worker result is checked below.
                        pass
            if events_path.exists():
                if events_path.stat().st_size > MAX_EVENT_BYTES:
                    raise ConversionError("파서 이벤트 크기 제한을 초과했습니다.")
                with events_path.open("rb") as stream:
                    stream.seek(event_offset)
                    data = stream.read(MAX_EVENT_BYTES + 1)
                    event_offset = stream.tell()
                pending += data
                lines = pending.split(b"\n")
                pending = lines.pop()
                for line in lines:
                    output(line.decode("utf-8"))

        report_stage("파서 작업자 실행 중")
        outcome = run_process(worker_command(request_path, report_path), timeout=None,
                              cwd=Path(__file__).resolve().parents[3], on_output=output,
                              memory_limit=WORKER_MEMORY_BYTES, on_poll=check_output)
        check_output()
        check_cancelled()
        if report_path.is_file() and report_path.stat().st_size <= 64 * 1024 * 1024:
            result = json.loads(report_path.read_text(encoding="utf-8"))
        else:
            result = {}
        if result.get("run_id") != identity:
            raise ConversionError("파서 작업자가 유효한 결과 없이 종료되었습니다. 메모리 제한/엔진 오류를 확인하세요.")
        if outcome.returncode or result.get("status") != "passed":
            error = result.get("error", "파서 작업자 오류")
            if result.get("error_type") == "JobTimedOut":
                raise JobTimedOut(error)
            if result.get("error_type") == "BookLimitError":
                raise BookLimitError(error)
            if operation == "raw":
                from magic_converter.core.jobs.raw_api import raise_error
                raise_error(result)
            raise ConversionError(error)
        check_output()
        if not result_path.exists() or output_state(result_path, max_bytes=output_limit(step.edge.engine_id)) != result.get("output"):
            raise ConversionError("파서 결과 파일의 무결성이 일치하지 않습니다.")
        yield result_path


def convert_isolated(step, destination, progress, notice):
    with isolated_result(step, destination.suffix, progress, notice) as result_path:
        if not result_path.is_file():
            from magic_converter.core.conversion.converter import ConversionError
            raise ConversionError("전문 파서가 단일 파일을 반환하지 않았습니다.")
        copy_file(result_path, destination)
        check_cancelled()


if __name__ == "__main__":
    raise SystemExit(helper_main(*sys.argv[1:]))
