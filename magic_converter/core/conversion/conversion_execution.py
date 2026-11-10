"""Execute registered routes through the existing, tested encoders."""

from __future__ import annotations

import tempfile
from pathlib import Path

from magic_converter.core.conversion.conversion_catalog import ConversionStep, Notice, Progress
from magic_converter.core.conversion.conversion_request import ConversionRequest, PlannedFile
from magic_converter.core.conversion.formats import FORMATS, IMAGE_EXTENSIONS
from magic_converter.core.jobs.job_control import check_cancelled, report_stage, copy_file, copy_tree
from magic_converter.core.settings.preferences import EncoderOptions, OutputRule


def run_legacy_action(step: ConversionStep, progress: Progress, notice: Notice) -> Path:
    from magic_converter.core.jobs.library_worker import needs_isolation, execute_isolated
    if needs_isolation(step):
        return execute_isolated(step, progress, notice)
    return run_local_action(step, progress, notice)


def run_local_action(step: ConversionStep, progress: Progress, notice: Notice) -> Path:
    # Lazy imports keep catalog and request construction usable without a QApplication.
    from magic_converter.core.conversion import converter as encoders
    from magic_converter.core.conversion.file_router import ALL_ACTIONS
    source, key = step.source, step.edge.action_key
    anchor = step.output_source or source
    from magic_converter.core.specialized.special_conversion import ENGINES, execute
    if step.edge.engine_id in ENGINES:
        return execute(step, progress, notice)
    if key.startswith("tool:"):
        from magic_converter.core.actions.tool_execution import execute_tool
        return execute_tool(step, progress, notice)
    if key == "copy_file":
        with encoders.output_file(anchor, FORMATS.get(step.edge.target).suffix) as (temporary, destination):
            copy_file(source, temporary)
            check_cancelled()
        return destination
    action = ALL_ACTIONS[key]
    extended = key.startswith(("document:", "office:", "sheet:", "subtitle:", "archive:")) and not (
        key == "document:pdf" and source.suffix.lower() in IMAGE_EXTENSIONS)
    extended |= key in {"pptx_image", "pptx_editable"}
    extended |= key == "strip" and source.suffix.lower() in {".pdf", ".docx", ".pptx", ".xlsx"}
    if extended:
        from magic_converter.core.conversion.extended_converter import convert_extended
        if step.output_source is None:
            return convert_extended(source, action, progress, notice)
        return convert_extended(source, action, progress, notice, output_source=anchor)
    suffix = action.suffix or source.suffix.lower()
    with encoders.output_file(anchor, suffix) as (temporary, destination):
        if step.edge.engine_id == "images" or source.suffix.lower() in IMAGE_EXTENSIONS:
            encoders.convert_image(source, temporary, suffix, compress=key == "compress",
                                   quality=step.options.image_quality, max_edge=step.options.image_max_edge)
        else:
            encoders.convert_media(source, temporary, key, step.ffmpeg, progress, options=step.options)
        check_cancelled()
    return destination


def execute_file(request: ConversionRequest, planned: PlannedFile, ffmpeg: str | None,
                 progress: Progress, notice: Notice) -> Path:
    from magic_converter.core.jobs.job_control import supervised_api
    with supervised_api():
        return _execute_file(request, planned, ffmpeg, progress, notice)


def _execute_file(request: ConversionRequest, planned: PlannedFile, ffmpeg: str | None,
                  progress: Progress, notice: Notice) -> Path:
    remote = next((item for item in request.web_inputs if item.placeholder == planned.source), None)
    if remote is not None:
        from dataclasses import replace
        from magic_converter.core.automation.web_transfer import materialize
        from magic_converter.core.conversion.builtin_converters import runtime_catalog
        from magic_converter.core.conversion.conversion_request import prepare_request
        with tempfile.TemporaryDirectory(prefix="magic-web-job-") as directory:
            source = materialize(remote, Path(directory) / "input", progress)
            # The server may send a different extension: re-plan from actual
            # bytes with the frozen action/options, never reinterpret the slice.
            actual = prepare_request([source], request.action.key, runtime_catalog(ffmpeg), output=request.output)
            if not {edge.engine_id for edge in actual.files[0].route.steps}.issubset({edge.engine_id for edge in planned.route.steps}):
                from magic_converter.core.conversion.converter import ConversionError
                raise ConversionError("웹 파일의 실제 형식이 예상과 달라 필요한 엔진이 바뀌었습니다. 파일을 저장한 뒤 다시 선택하세요.")
            actual = replace(actual, action=request.action, preset_id=request.preset_id, preset_name=request.preset_name,
                             options=request.options, tools=request.tools)
            return execute_file(actual, actual.files[0], ffmpeg,
                                lambda p: progress(None if p is None else 20 + p * 80 // 100), notice)
    from magic_converter.core.conversion.converter import ConversionError, output_file, publish_folder
    steps = planned.route.steps
    if not steps:
        raise ConversionError("이 작업은 파일별 변환 경로가 아닙니다.")
    check_cancelled()
    if (len(steps) == 1 and request.output == OutputRule() and request.options == EncoderOptions()
            and not request.preset_id):
        edge = steps[0]
        return request.converter(edge.engine_id).convert(ConversionStep(planned.source, edge, ffmpeg, tools=request.tools), progress, notice)

    # All intermediate AND final encoding happens in a private directory. Only the
    # finished final result is copied through the existing atomic output publisher.
    with tempfile.TemporaryDirectory(prefix="magic-route-") as directory:
        workspace = Path(directory).resolve()
        source = planned.source
        for index, edge in enumerate(steps):
            check_cancelled()
            report_stage(f"변환 {index + 1}/{len(steps)} · {edge.target.upper()}")
            stage = workspace / str(index)
            stage.mkdir()
            anchor = stage / planned.source.name

            def update(percent: int | None, hop: int = index) -> None:
                progress(None if percent is None else min(99, (hop * 100 + percent) // len(steps)))

            source = request.converter(edge.engine_id).convert(
                ConversionStep(source, edge, ffmpeg, anchor, request.options, request.tools), update, notice)
            source = Path(source).resolve()
            if not source.is_relative_to(workspace) or not source.exists():
                raise ConversionError("변환 엔진이 작업 폴더에 결과를 생성하지 않았습니다.")
            if index < len(steps) - 1 and not source.is_file():
                raise ConversionError("여러 파일로 나뉜 중간 결과는 다음 변환에 사용할 수 없습니다.")
        check_cancelled()
        if source.is_dir():
            def produce(folder: Path) -> None:
                copy_tree(source, folder)
                check_cancelled()
            return publish_folder(planned.source, produce, rule=request.output, preset_name=request.preset_name,
                                  allow_empty=request.action.key == "tool:archive_extract")
        tag = "slide" if request.action.key == "pptx_image" else "editable" if request.action.key == "pptx_editable" else "converted"
        with output_file(planned.source, FORMATS.get(steps[-1].target).suffix, tag=tag,
                         rule=request.output, preset_name=request.preset_name) as (temporary, destination):
            copy_file(source, temporary)
            check_cancelled()
        return destination
