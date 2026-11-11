"""Register existing encoders without loading Qt or probing the user's PC."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from magic_converter.core.conversion.conversion_catalog import (ActionPolicy, ConversionEdge, ConversionStep, ConverterCatalog,
                                 EngineStatus, Notice, Progress)
from magic_converter.core.conversion.formats import FORMATS


@dataclass(frozen=True)
class BuiltInConverter:
    id: str
    display_name: str
    edges: tuple[ConversionEdge, ...]
    status: EngineStatus = EngineStatus()
    max_parallelism: int = 2

    def convert(self, step: ConversionStep, progress: Progress, notice: Notice) -> Path:
        from magic_converter.core.conversion.conversion_execution import run_legacy_action
        from magic_converter.core.jobs.job_control import supervised_api
        with supervised_api():
            return run_legacy_action(step, progress, notice)


def create_catalog(*, ffmpeg_available: bool = True, presentation_available: bool = True,
                   rhwp_available: bool = False) -> ConverterCatalog:
    from magic_converter.core.engines.office_formats import WORD, SLIDES, SHEETS
    word_ids, slide_ids, sheet_ids = ({s[1:] for s in family} for family in (WORD, SLIDES, SHEETS))
    office_ids = word_ids | slide_ids | sheet_ids
    images = FORMATS.ids("image")
    videos, audio = FORMATS.ids("video"), FORMATS.ids("audio")
    media = videos | audio
    documents = frozenset(item.id for item in FORMATS.known
                          if item.category == "document" and item.readable)
    hwp, slides, sheets = FORMATS.ids("hwp"), FORMATS.ids("slide"), FORMATS.ids("sheet")
    archives, subtitles = FORMATS.ids("archive"), FORMATS.ids("subtitle")
    image_targets = images - {"svg"}
    document_targets = ("docx", "md", "txt", "epub", "html", "pdf")
    sheet_targets = ("xlsx", "ods", "csv", "tsv")
    edges: dict[str, list[ConversionEdge]] = {}

    def add(engine: str, source: str, target: str, key: str, **options) -> None:
        edges.setdefault(engine, []).append(ConversionEdge(source, target, engine, key, **options))

    for source in sorted(images):
        for target in sorted(image_targets):
            add("images", source, target, f"image:{target}")
        add("images", source, "pdf", "document:pdf", terminal_only=True)
        if source != "svg":
            add("images", source, source, "strip", intent="strip", direct_only=True)
            add("images", source, source, "compress", intent="compress", direct_only=True)
    for target in ("png", "webp"):
        add("images", "gif", target, f"image:{target}", direct_only=True)
    for source in sorted(media):
        for target in sorted(videos | audio):
            if target == "gif" and source not in videos or target in audio and source == "gif":
                continue
            key = f"media:{target}" if target in videos or target == "mp3" else f"audio:{target}"
            add("ffmpeg", source, target, key, direct_only=True)
        add("ffmpeg", source, source, "strip", intent="strip", direct_only=True)
    for target in ("docx", "xlsx", "epub", "txt", "md", "html"):
        add("pdf", "pdf", target, f"document:{target}",
            terminal_only=target != "docx", cost=16 if target == "docx" else 10)
    for target in ("jpg", "png"):
        add("pdf", "pdf", target, f"document:{target}", terminal_only=True)
    for key in ("pptx_image", "pptx_editable"):
        add("pdf", "pdf", "pptx", key, intent=key, direct_only=True)
    add("pdf", "pdf", "pdf", "strip", intent="strip", direct_only=True)
    add("copy", "pdf", "pdf", "copy_file", direct_only=True)
    for source in sorted(documents):
        for target in document_targets:
            if source == "epub" or target == "epub" and source in {"docx", "txt", "md"}:
                continue  # Rich chapter/metadata/image model owns these routes.
            add("documents", source, target, f"document:{target}",
                terminal_only=target not in {"docx", "pdf"},
                office_queue=source == "docx" and target == "pdf")
    for source in sorted(sheets):
        for target in sheet_targets:
            add("spreadsheets", source, target, f"sheet:{target}", terminal_only=True)
        add("spreadsheets", source, "pdf", "document:pdf", office_queue=source == "xlsx")
    add("spreadsheets", "xlsx", "pdf", "office:xlsx_print_pdf", intent="office:xlsx_print_pdf",
        direct_only=True, office_queue=True)
    for source in sorted(slides):
        add("presentations", source, "pdf", "document:pdf", office_queue=True)
        if source != "ppt":
            add("documents", source, "pptx", "office:pptx", direct_only=True)
    for source in sorted(hwp):
        for target in ("docx", "pdf", "txt"):
            add("hwp_documents", source, target, f"document:{target}",
                office_queue=True, terminal_only=target == "txt")
    add("hwp_documents", "hwp", "hwpx", "document:hwpx", office_queue=True)
    for source in sorted(office_ids):
        add("hwp_documents", source, "hwpx", "document:hwpx", office_queue=True)
    for source in sorted({"hwpx"} | office_ids):
        add("rhwp", source, "hwp", "document:hwp", office_queue=True, direct_only=True)
    for source in ("docx", "pptx", "xlsx"):
        add("metadata", source, source, "strip", intent="strip", direct_only=True)
    for source in sorted(subtitles):
        for target in ("srt", "vtt", "txt"):
            add("subtitles", source, target, f"subtitle:{target}", terminal_only=True)
    for source in sorted(archives):
        for target, key in (("zip", "zip"), ("tar", "tar"), ("tar.gz", "gzip")):
            add("archives", source, target, f"archive:{key}", direct_only=True)

    policies = {f"image:{target}": ActionPolicy(target, images) for target in image_targets}
    for target in ("png", "webp"):
        policies[f"image:{target}"] = ActionPolicy(target, images | {"gif"})
    for target in videos | audio:
        key = f"media:{target}" if target in videos or target == "mp3" else f"audio:{target}"
        sources = videos if target == "gif" else media - {"gif"} if target in audio else media
        policies[key] = ActionPolicy(target, sources)
    for target in document_targets:
        sources = (images | documents | hwp | slides | sheets | {"pdf"} if target == "pdf"
                   else documents | hwp | {"pdf"})
        policies[f"document:{target}"] = ActionPolicy(target, frozenset(sources))
    for target in ("jpg", "png"):
        policies[f"document:{target}"] = ActionPolicy(target, frozenset({"pdf"}) | slides | hwp | documents | sheets)
    policies["document:xlsx"] = ActionPolicy("xlsx", frozenset({"pdf"}))
    policies["document:hwp"] = ActionPolicy("hwp", frozenset({"hwpx"} | office_ids))
    policies["document:hwpx"] = ActionPolicy("hwpx", frozenset({"hwp"} | office_ids))
    policies["office:pptx"] = ActionPolicy("pptx", frozenset(slide_ids))
    policies["office:xlsx_print_pdf"] = ActionPolicy("pdf", frozenset({"xlsx"}), intent="office:xlsx_print_pdf")
    for key in ("pptx_image", "pptx_editable"):
        policies[key] = ActionPolicy("pptx", frozenset({"pdf"}), intent=key)
    policies.update({f"sheet:{target}": ActionPolicy(target, sheets) for target in sheet_targets})
    policies.update({f"subtitle:{target}": ActionPolicy(target, subtitles) for target in ("srt", "vtt", "txt")})
    policies.update({f"archive:{key}": ActionPolicy(target, archives)
                     for target, key in (("zip", "zip"), ("tar", "tar"), ("tar.gz", "gzip"))})
    policies["compress"] = ActionPolicy(None, images - {"svg"}, intent="compress")
    policies["strip"] = ActionPolicy(None, (images - {"svg"}) | media | {"pdf", "docx", "pptx", "xlsx"}, intent="strip")
    policies.update({key: ActionPolicy(None, batch=True) for key in ("copy_path", "zip", "terminal")})
    from magic_converter.core.specialized.special_formats import routes
    for engine, source, target, key in routes():
        add(engine, source, target, key, direct_only=not (engine == "ebook" and target == "docx"))
        old = policies.get(key)
        policies[key] = ActionPolicy(target, frozenset({source}) | (old.sources if old and old.sources else frozenset()))
    # FB2 can use the existing DOCX→PDF renderer; queue accounting reserves its
    # Office gate for the entire immutable route, as for EPUB.
    for key in ("document:pdf", "document:jpg", "document:png"):
        old = policies[key]
        policies[key] = ActionPolicy(old.target, old.sources | {"fb2"})
    from magic_converter.core.actions.tool_definitions import TOOLS
    for tool in TOOLS:
        policies[tool.key] = ActionPolicy(tool.target, tool.sources, intent=tool.key)
        for source in sorted(tool.sources):
            add(tool.engine, source, tool.target or source, tool.key, intent=tool.key,
                direct_only=True, terminal_only=tool.folder)

    statuses = {
        "ffmpeg": EngineStatus(ffmpeg_available, "" if ffmpeg_available else "FFmpeg를 설치하거나 설정에서 경로를 지정하세요"),
        "presentations": EngineStatus(presentation_available, "" if presentation_available else "PowerPoint/LibreOffice 필요"),
        "rhwp": EngineStatus(rhwp_available, "" if rhwp_available else "rhwp 엔진 필요"),
    }
    names = {"images": "이미지", "ffmpeg": "FFmpeg", "pdf": "PDF", "copy": "파일 복사",
             "documents": "문서", "spreadsheets": "스프레드시트", "presentations": "PowerPoint/LibreOffice",
             "hwp_documents": "한글 문서 (rhwp/문서 모델)", "rhwp": "rhwp",
             "metadata": "문서 메타데이터", "subtitles": "자막", "archives": "압축",
             "pdf_tools": "PDF 도구", "image_tools": "이미지 도구", "subtitle_tools": "자막 도구", "archive_tools": "압축 도구"}
    names.update(data="구조화 데이터", font="폰트 (fontTools)", ebook="전자책·만화", email="메일·웹 아카이브",
                 mesh="3D 삼각형 메시", geo="WGS84 지리정보")
    return ConverterCatalog((BuiltInConverter(engine, names[engine], tuple(items), statuses.get(engine, EngineStatus()),
                                              1 if engine in {"pdf", "pdf_tools", "presentations", "hwp_documents", "rhwp"} else 2)
                             for engine, items in edges.items()), policies=policies)


def runtime_catalog(ffmpeg: str | None) -> ConverterCatalog:
    from magic_converter.core.engines.office_engine import presentation_available
    from magic_converter.core.engines.rhwp_engine import locate
    return create_catalog(ffmpeg_available=ffmpeg is not None,
                          presentation_available=presentation_available(),
                          rhwp_available=locate() is not None)
