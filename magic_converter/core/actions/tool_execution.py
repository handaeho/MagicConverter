"""Run tools through the same atomic output publishers as conversions."""

from magic_converter.core.conversion.formats import FORMATS
from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.actions.tool_definitions import TOOL_BY_KEY


def execute_tool(step, progress, notice):
    from magic_converter.core.conversion.converter import output_file, publish_folder
    tool = TOOL_BY_KEY[step.edge.action_key]
    source, anchor, options = step.source, step.output_source or step.source, step.tools
    progress(0)
    if tool.folder:
        def produce(folder):
            if tool.key == "tool:pdf_split":
                from magic_converter.core.pdf.pdf_tools import split_pdf
                split_pdf(source, folder, options, progress)
            elif tool.key == "tool:image_frames":
                from magic_converter.core.media.image_tools import extract_image_frames
                extract_image_frames(source, folder, options, progress)
            elif tool.key == "tool:media_frames":
                from magic_converter.core.media.media_tools import extract_media_frames
                extract_media_frames(source, folder, options, step.ffmpeg, progress)
            else:
                from magic_converter.core.archives.archive_converter import extract_archive
                extract_archive(source, folder, progress)
            check_cancelled()
        return publish_folder(anchor, produce, allow_empty=tool.key == "tool:archive_extract")
    with output_file(anchor, FORMATS.get(step.edge.target).suffix) as (temporary, destination):
        if tool.key.startswith("tool:pdf_"):
            from magic_converter.core.pdf.pdf_tools import transform_pdf
            transform_pdf(source, temporary, tool.key, options, progress, notice)
        elif tool.key.startswith("tool:image_"):
            from magic_converter.core.media.image_tools import transform_image
            transform_image(source, temporary, tool.key, options, progress)
        elif tool.key.startswith("tool:media_"):
            from magic_converter.core.media.media_tools import convert_media_tool
            convert_media_tool(source, temporary, tool.key, options, step.ffmpeg, progress, notice)
        else:
            from magic_converter.core.media.subtitle_converter import convert_subtitle
            convert_subtitle(source, temporary, source.suffix.lower(), offset_ms=options.subtitle_offset_ms, notice=notice)
        check_cancelled()
    return destination
