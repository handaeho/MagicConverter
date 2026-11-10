"""Owned encoding for combined PDF/ZIP requests; only the parent publishes."""

from pathlib import Path
from contextvars import ContextVar
from contextlib import contextmanager
from threading import Event
import time

from magic_converter.core.jobs.job_control import check_cancelled, copy_file
from magic_converter.core.jobs.specialized_worker import isolated_result, output_state, output_limit

local_encoding = ContextVar('magic_combined_local_encoding', default=False)
DEFAULT_TIMEOUT = 3600


@contextmanager
def _supervision():
    from magic_converter.core.jobs.job_control import Control, current
    token = None
    if current.get() is None:
        token = current.set(Control(Event(), lambda _: None, time.monotonic() + DEFAULT_TIMEOUT))
    try:
        yield
    finally:
        if token is not None:
            current.reset(token)


def run_request(request, destination, progress):
    from PySide6.QtGui import QGuiApplication
    from magic_converter.core.conversion.conversion_request import ConversionRequest, PlannedFile
    from magic_converter.core.conversion.conversion_catalog import Route
    from magic_converter.core.conversion.file_router import ALL_ACTIONS
    from magic_converter.core.platform.headless_qt import configure_headless_qt
    from magic_converter.core.settings.preferences import OutputRule

    configure_headless_qt()
    app = QGuiApplication.instance() or QGuiApplication([])
    key = request["action_key"]
    if key not in {"zip", "tool:pdf_merge"}:
        raise ValueError("Unknown combined worker action")
    paths = tuple(Path(value) for value in request["sources"])
    if not paths or paths[0] != Path(request["source"]):
        raise ValueError("Invalid combined source order")
    folder = destination.parent / "output"
    folder.mkdir()
    rule = OutputRule("folder", str(folder))
    token = local_encoding.set(True)
    try:
        if key == "zip":
            from magic_converter.core.conversion.converter import create_zip
            result = create_zip(paths, rule=rule)
        else:
            from magic_converter.core.pdf.pdf_tools import merge_pdf
            plan = ConversionRequest(ALL_ACTIONS[key], tuple(PlannedFile(p, Route((), 0)) for p in paths), (), output=rule)
            result = merge_pdf(plan, progress)
    finally:
        local_encoding.reset(token)
    if not result.resolve().is_relative_to(folder):
        raise ValueError("Combined result escapes its private directory")
    output_state(result, max_bytes=output_limit(request["engine"]))
    result.replace(destination)


def execute(request, progress, notice):
    with _supervision():
        return _execute(request, progress, notice)


def _execute(request, progress, notice):
    from magic_converter.core.conversion.conversion_catalog import ConversionEdge, ConversionStep
    from magic_converter.core.conversion.converter import output_file
    from magic_converter.core.conversion.formats import FORMATS

    key = request.action.key
    engine, target = ("archives", "zip") if key == "zip" else ("pdf_tools", "pdf")
    source_format = FORMATS.detect(request.paths[0])
    step = ConversionStep(request.paths[0], ConversionEdge(source_format.id if source_format else "binary", target, engine, key))
    suffix = "." + target
    fields = {"sources": [str(p.resolve()) for p in request.paths], "action_key": key}
    with isolated_result(step, suffix, progress, notice, operation="combined", fields=fields) as result:
        if not result.is_file():
            raise ValueError("Combined worker did not return one file")
        with output_file(request.paths[0], suffix, rule=request.output,
                         preset_name=request.preset_name) as (temporary, destination):
            copy_file(result, temporary)
            check_cancelled()
        return destination
