"""Keep COM ownership in the supervisor while document libraries run in workers."""

from contextlib import contextmanager
from contextvars import ContextVar
import json
import re
import stat
import tempfile
import time
import uuid
from pathlib import Path

from magic_converter.core.jobs.job_control import check_cancelled, JobCancelled, JobTimedOut

native_proxy = ContextVar("magic_converter_native_proxy", default=None)


def write_json(path, value):
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    temporary.replace(path)


@contextmanager
def client(folder, run_id, emit):
    directory = folder / "native"
    directory.mkdir()

    def request(source, destination, operation):
        check_cancelled()
        identity = uuid.uuid4().hex
        write_json(directory / (identity + ".request.json"), {
            "run_id": run_id, "operation": operation,
            "source": str(source.resolve()), "destination": str(destination.resolve())})
        emit(identity)
        reply = directory / (identity + ".reply.json")
        while not reply.is_file():
            check_cancelled()
            time.sleep(.05)
        outcome = json.loads(reply.read_text(encoding="utf-8"))
        if outcome.get("run_id") != run_id:
            raise ValueError("Native reply belongs to another worker run")
        if not outcome.get("ok"):
            from magic_converter.core.conversion.converter import ConversionError
            raise ConversionError(outcome.get("error", "Native export failed"))
        check_cancelled()

    # Only the single-threaded worker changes its temp root. Intermediates stay
    # inside the parent's private scope, including Office variant normalization.
    scratch = folder / "scratch"
    scratch.mkdir()
    previous_temp = tempfile.tempdir
    token = native_proxy.set(request)
    try:
        tempfile.tempdir = str(scratch)
        yield
    finally:
        tempfile.tempdir = previous_temp
        native_proxy.reset(token)


def private_path(value, folder):
    path = Path(value).absolute()
    roots = (folder / "output", folder / "scratch")
    if not any(path != root and path.is_relative_to(root) for root in roots):
        raise ValueError("Native path escapes private output/scratch")
    item = path
    while item != folder:
        if item.exists() or item.is_symlink():
            info = item.lstat()
            if item.is_symlink() or getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise ValueError("Native path contains a link/reparse point")
        item = item.parent
    resolved = path.resolve()
    if resolved != path:
        raise ValueError("Native path is not canonical")
    return resolved


def handle_request(folder, run_id, original, identity, handled):
    from magic_converter.core.engines.hwp_converter import run_com

    if not isinstance(identity, str) or not re.fullmatch(r"[0-9a-f]{32}", identity) or identity in handled:
        raise ValueError("Invalid/replayed native request")
    handled.add(identity)
    path = folder / "native" / (identity + ".request.json")
    if path.is_symlink() or path.stat().st_size > 16384:
        raise ValueError("Unsafe native request")
    request = json.loads(path.read_text(encoding="utf-8"))
    operation = request.get("operation")
    if request.get("run_id") != run_id or operation not in {"office_pdf", "excel_print"}:
        raise ValueError("Invalid native request identity/operation")
    source = Path(request["source"]).absolute()
    if source != original.resolve():
        source = private_path(str(source), folder)
    destination = private_path(request["destination"], folder)
    if not source.is_file() or not destination.parent.is_dir() or destination == source:
        raise ValueError("Invalid native input/output")
    if operation == "excel_print" and source.suffix.lower() != ".xlsx":
        raise ValueError("Unsupported Excel print input suffix")
    allowed = {".pdf"} if operation in {"office_pdf", "excel_print"} else {".pdf", ".hwp", ".hwpx", ".docx"}
    if destination.suffix.lower() not in allowed:
        raise ValueError("Unsupported native output suffix")
    try:
        # Validation is a library operation too: run it back in the worker.
        # Native helper/ledger cleanup stays in this original supervisor frame.
        run_com(source, destination, operation, validate=False)
        result = {"ok": True, "run_id": run_id}
    except (JobCancelled, JobTimedOut):
        raise
    except Exception as exc:
        result = {"ok": False, "run_id": run_id, "error": str(exc)[:2000]}
    write_json(folder / "native" / (identity + ".reply.json"), result)
