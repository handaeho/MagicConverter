"""Finite, owned boundaries for public data/image/document-model functions."""

import base64
from contextvars import ContextVar
from dataclasses import fields, is_dataclass
from functools import wraps
import inspect
import json
import os
from pathlib import Path
import tempfile
from types import SimpleNamespace

from magic_converter.core.jobs.job_control import check_cancelled, copy_file, supervised_api

in_worker = ContextVar("magic_converter_raw_worker", default=False)
MAX_WIRE_BYTES = 256 * 1024 * 1024
MAX_NODES = 2000000  # Includes mapping keys and model field wrappers.


def model_types():
    from magic_converter.core.documents import hwp_documents as model
    return {name: getattr(model, name) for name in
            ("DocumentData", "Paragraph", "TextRun", "Picture", "Equation", "Table", "Cell")}


def encode(value):
    """An explicit, bounded JSON tree; never unpickle worker-controlled data."""
    count, size = 0, 0
    models = model_types()

    def visit(item, depth=0):
        nonlocal count, size
        count += 1
        if count % 256 == 0:
            check_cancelled()
        if count > MAX_NODES or depth > 128 or size > MAX_WIRE_BYTES:
            raise ValueError("Raw API model transfer budget exceeded")
        if type(item) in (str, int, float, bool, type(None)):
            if isinstance(item, str):
                size += len(item) * 6  # Conservative escaped JSON upper bound.
            return ["value", item]
        if isinstance(item, Path):
            return ["path", str(item.resolve())]
        if isinstance(item, bytes):
            size += (len(item) + 2) // 3 * 4
            if size > MAX_WIRE_BYTES:
                raise ValueError("Raw API model transfer budget exceeded")
            return ["bytes", base64.b64encode(item).decode("ascii")]
        if type(item) in (list, tuple):
            return ["tuple" if isinstance(item, tuple) else "list", [visit(x, depth+1) for x in item]]
        if type(item) is dict:
            return ["dict", [[visit(k, depth+1), visit(v, depth+1)] for k, v in item.items()]]
        name = type(item).__name__
        if is_dataclass(item) and models.get(name) is type(item):
            return [name, {field.name: visit(getattr(item, field.name), depth+1) for field in fields(item)}]
        raise TypeError("Unsupported raw API model type: " + name)

    result = visit(value)
    if size > MAX_WIRE_BYTES:
        raise ValueError("Raw API model transfer budget exceeded")
    return result


def decode(value):
    models = model_types()
    count = 0

    def visit(node, depth=0):
        nonlocal count
        count += 1
        if count % 256 == 0:
            check_cancelled()
        if count > MAX_NODES or depth > 128 or not isinstance(node, list) or len(node) != 2:
            raise ValueError("Invalid raw API model")
        tag, item = node
        if tag == "value" and type(item) in (str, int, float, bool, type(None)):
            return item
        if tag == "path" and isinstance(item, str):
            return Path(item)
        if tag == "bytes" and isinstance(item, str):
            return base64.b64decode(item, validate=True)
        if tag in {"list", "tuple"} and isinstance(item, list):
            result = [visit(x, depth+1) for x in item]
            return tuple(result) if tag == "tuple" else result
        if tag == "dict" and isinstance(item, list):
            result = {}
            for key, val in item:
                key = visit(key, depth+1)
                if key in result:
                    raise ValueError("Duplicate raw API model key")
                result[key] = visit(val, depth+1)
            return result
        if tag in models and isinstance(item, dict) and set(item) == {f.name for f in fields(models[tag])}:
            return models[tag](**{k: visit(v, depth+1) for k, v in item.items()})
        raise ValueError("Invalid raw API model tag")

    return visit(value)


def write_wire(path, value):
    encoded = json.dumps(encode(value), ensure_ascii=True, allow_nan=False, separators=(",", ":"))
    if len(encoded) > MAX_WIRE_BYTES:
        raise ValueError("Raw API model transfer budget exceeded")
    path.write_text(encoded, encoding="utf-8")


def read_wire(path):
    if path.stat().st_size > MAX_WIRE_BYTES:
        raise ValueError("Raw API model transfer budget exceeded")
    return decode(json.loads(path.read_text(encoding="utf-8")))


def bounded_raw(function):
    signature = inspect.signature(function)

    @wraps(function)
    def call(*args, **kwargs):
        if in_worker.get():
            return function(*args, **kwargs)
        from magic_converter.core.jobs.specialized_worker import isolated_result, _ProcessDirectory
        with supervised_api():
            check_cancelled()
            bound = signature.bind(*args, **kwargs)
            bound.apply_defaults()
            values = dict(bound.arguments)
            notice = values.pop("notice", lambda _: None)
            destination = values.pop("destination", None)
            with _ProcessDirectory(prefix="magic-raw-input-") as directory:
                source = Path(directory) / "arguments.json"
                write_wire(source, values)
                edge = SimpleNamespace(engine_id="raw_api", source="raw", target="raw")
                step = SimpleNamespace(source=source, edge=edge)
                fields = {"function": function.__module__ + ":" + function.__name__,
                          "file_result": destination is not None}
                suffix = Path(destination).suffix if destination is not None else ".json"
                with isolated_result(step, suffix, lambda _: None, notice, operation="raw", fields=fields) as result:
                    if destination is None:
                        return read_wire(result)
                    destination = Path(destination)
                    # Publish to the exact caller path, preserving its overwrite
                    # semantics. Failures leave an existing file untouched.
                    fd, name = tempfile.mkstemp(prefix=".magic-raw-", suffix=suffix, dir=destination.parent)
                    os.close(fd)
                    temporary = Path(name)
                    try:
                        copy_file(result, temporary)
                        check_cancelled()
                        os.replace(temporary, destination)
                    finally:
                        temporary.unlink(missing_ok=True)
                return None
    return call


def run_request(request, destination, notice):
    """Only the named facade/rich functions are callable, with no input imports."""
    from magic_converter.core.specialized import data_converter
    from magic_converter.core.conversion import converter
    from magic_converter.core.documents import hwp_documents
    from magic_converter.core.documents import rich_hwp
    functions = [data_converter.read_data, converter.convert_image,
                 *(getattr(hwp_documents, name) for name in
                   ("read_docx", "read_hwpx", "read_hwp", "write_docx", "write_hwpx")),
                 *(getattr(rich_hwp, name) for name in
                   ("read_docx_rich", "read_hwpx_rich", "read_hwp_rich", "write_docx_rich", "document_html_rich"))]
    allowed = {f.__module__ + ":" + f.__name__: f for f in functions}
    function = allowed.get(request["function"])
    if function is None:
        raise ValueError("Unknown raw API function")
    values = read_wire(Path(request["source"]))
    file_result = "destination" in inspect.signature(function).parameters
    if request["file_result"] is not file_result or not isinstance(values, dict) or "destination" in values:
        raise ValueError("Invalid raw API output contract")
    if "notice" in inspect.signature(function).parameters:
        values["notice"] = notice
    if file_result:
        values["destination"] = destination
    app = None
    if function is converter.convert_image and Path(values["source"]).suffix.lower() == ".svg":
        # SVG text needs Qt's font database in a fresh headless raw worker,
        # just as it does in the registered library worker.
        from PySide6.QtGui import QGuiApplication
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
        app = QGuiApplication.instance() or QGuiApplication([])
    result = function(**values)
    if not file_result:
        write_wire(destination, result)


def error_details(error):
    if isinstance(error, json.JSONDecodeError):
        return {"msg": error.msg, "doc": error.doc, "pos": error.pos}
    if isinstance(error, OSError):
        return {"errno": error.errno, "strerror": error.strerror, "filename": error.filename}
    return {"args": error.args} if all(type(x) in (str, int, float, bool, type(None)) for x in error.args) else {}


def raise_error(result):
    kind, detail = result.get("error_type"), result.get("error_details", {})
    if kind == "JSONDecodeError" and set(detail) == {"msg", "doc", "pos"}:
        raise json.JSONDecodeError(**detail)
    import builtins
    allowed = {"ValueError", "TypeError", "KeyError", "FileNotFoundError", "PermissionError",
               "IsADirectoryError", "NotADirectoryError", "OSError"}
    if kind in allowed:
        cls = getattr(builtins, kind)
        if issubclass(cls, OSError) and "errno" in detail:
            raise cls(detail["errno"], detail["strerror"], detail["filename"])
        raise cls(*detail.get("args", [result.get("error", kind)]))
