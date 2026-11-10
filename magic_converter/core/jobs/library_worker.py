"""Owned worker boundary for local PDF/document/image library routes."""

from dataclasses import asdict
from pathlib import Path

from magic_converter.core.jobs.job_control import check_cancelled, copy_file, copy_tree
from magic_converter.core.jobs.specialized_worker import isolated_result, output_state, output_limit

ENGINES = frozenset({"images", "pdf", "documents", "spreadsheets", "metadata", "pdf_tools", "image_tools",
                     "presentations", "hwp_documents", "rhwp", "archives", "archive_tools",
                     "subtitles", "subtitle_tools"})


def needs_isolation(step):
    return step.edge.engine_id in ENGINES


def run_request(request, destination, progress, notice, office_event):
    from magic_converter.core.conversion.conversion_catalog import ConversionEdge, ConversionStep
    from magic_converter.core.conversion.conversion_execution import run_local_action
    from magic_converter.core.settings.preferences import EncoderOptions, ToolOptions

    folder = destination.parent / "output"
    folder.mkdir()
    source = Path(request["source"])
    step = ConversionStep(source, ConversionEdge(**request["edge"]), request.get("ffmpeg"),
                          folder / source.name, EncoderOptions(**request["options"]), ToolOptions(**request["tools"]))
    if not needs_isolation(step):
        raise ValueError("Unsupported local library route")
    # TXT -> MD has no layout or markup parsing. Keep it inside this same
    # bounded process, without initializing Qt fonts and rich encoders.
    if (step.edge.engine_id == 'documents' and step.edge.source == 'txt'
            and source.suffix.lower() == '.txt' and step.edge.target == 'md'
            and step.edge.action_key == 'document:md'):
        from magic_converter.core.documents.text_codec import decode_text
        check_cancelled()
        text = decode_text(source.read_bytes())
        progress(40)
        check_cancelled()
        result = folder / (source.stem + '.md')
        result.write_text(text, encoding='utf-8')
        check_cancelled()
        progress(100)
    else:
        from PySide6.QtGui import QGuiApplication
        from magic_converter.core.platform.headless_qt import configure_headless_qt
        configure_headless_qt()
        app = QGuiApplication.instance() or QGuiApplication([])
        from magic_converter.core.jobs.office_broker import client
        with client(destination.parent, request["run_id"], office_event):
            result = run_local_action(step, progress, notice)
    if not result.resolve().is_relative_to(folder.resolve()):
        raise ValueError("Library result escapes its private output directory")
    output_state(result, max_bytes=output_limit(step.edge.engine_id))
    result.replace(destination)


def execute_isolated(step, progress, notice):
    from magic_converter.core.conversion.converter import output_file, publish_folder
    from magic_converter.core.conversion.formats import FORMATS

    fields = {"edge": asdict(step.edge), "options": asdict(step.options),
              "tools": asdict(step.tools), "ffmpeg": step.ffmpeg}
    suffix = FORMATS.get(step.edge.target).suffix
    anchor = step.output_source or step.source
    with isolated_result(step, suffix, progress, notice, operation="library", fields=fields) as result:
        if result.is_dir():
            def produce(folder):
                copy_tree(result, folder)
                check_cancelled()
            return publish_folder(anchor, produce, allow_empty=step.edge.action_key == "tool:archive_extract")
        key = step.edge.action_key
        tag = "slide" if key == "pptx_image" else "editable" if key == "pptx_editable" else "converted"
        with output_file(anchor, suffix, tag=tag) as (temporary, destination):
            copy_file(result, temporary)
            check_cancelled()
        return destination
