"""Pure routing rules, shared by the overlay and the worker.

Extensions determine eligibility; an encoder validates the actual file contents.
For a batch, an action must support every input. No partial, silent filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from magic_converter.core.settings.preferences import Preset

from magic_converter.core.conversion.formats import (ARCHIVE_EXTENSIONS, AUDIO_EXTENSIONS, DOCUMENT_EXTENSIONS,
                      HWP_EXTENSIONS, IMAGE_EXTENSIONS, MEDIA_EXTENSIONS,
                      SHEET_EXTENSIONS, SLIDE_EXTENSIONS, VIDEO_EXTENSIONS)
from magic_converter.core.actions.tool_definitions import TOOLS

Mode = Literal["convert", "action"]


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    description: str
    suffix: str = ""


CONVERT_ACTIONS = (
    Action("image:png", "PNG", "PNG 이미지로 변환", ".png"),
    Action("image:jpg", "JPG", "JPG 이미지로 변환 (투명 영역은 흰색)", ".jpg"),
    Action("image:webp", "WebP", "WebP 이미지로 변환", ".webp"),
    Action("image:bmp", "BMP", "BMP 이미지로 변환", ".bmp"),
    Action("image:tiff", "TIFF", "TIFF 이미지로 변환", ".tiff"),
    Action("image:heic", "HEIC", "HEIC 이미지로 변환", ".heic"),
    Action("image:avif", "AVIF", "AVIF 이미지로 변환", ".avif"),
    Action("compress", "50% 압축", "품질·해상도를 조정해 파일 크기를 절반 이하로 축소"),
    Action("media:mp4", "MP4", "H.264/AAC MP4 (오디오는 정지 화면 포함)", ".mp4"),
    Action("media:gif", "GIF", "팔레트 기반 고화질 GIF", ".gif"),
    Action("media:mp3", "MP3", "MP3 오디오 변환·추출", ".mp3"),
    Action("media:webm", "WebM", "VP9/Opus WebM (오디오는 정지 화면 포함)", ".webm"),
    Action("media:mov", "MOV", "H.264/AAC MOV", ".mov"),
    Action("media:mkv", "MKV", "H.264/AAC Matroska", ".mkv"),
    Action("media:avi", "AVI", "MPEG-4/MP3 AVI", ".avi"),
    Action("media:wmv", "WMV", "WMV2/WMA 영상", ".wmv"),
    *(Action(f"audio:{ext}", label, f"{label} 오디오로 변환", f".{ext}") for ext, label in
      (("m4a", "M4A"), ("wav", "WAV"), ("flac", "FLAC"), ("ogg", "OGG"),
       ("opus", "Opus"), ("aiff", "AIFF"), ("wma", "WMA"))),
    *(Action(f"document:{ext}", label, description, f".{ext}") for ext, label, description in (
        ("pdf", "PDF", "PDF로 내보내기"), ("docx", "DOCX", "DOCX로 변환 · PDF의 검출된 표와 이미지 보존"),
        ("xlsx", "XLSX", "PDF 표·텍스트를 페이지별 시트로 추출"),
        ("jpg", "JPG", "모든 PDF 페이지/슬라이드를 300 DPI 이미지로 출력"),
        ("png", "PNG", "모든 PDF 페이지/슬라이드를 300 DPI 이미지로 출력"),
        ("epub", "EPUB", "전자책 · 지원 입력의 장·메타데이터·내장 이미지 보존"), ("txt", "TXT", "텍스트 추출"),
        ("md", "MD", "Markdown 텍스트로 변환"), ("html", "HTML", "HTML 문서로 내보내기"))),
    Action("pptx_image", "PPTX (고정)", "200 DPI 통이미지 슬라이드로 모양 유지", ".pptx"),
    Action("pptx_editable", "PPTX (편집)", "검출된 표는 실제 셀로, 본문은 편집 가능한 텍스트로 변환", ".pptx"),
    Action("office:pptx", "PPTX", "Office 슬라이드·템플릿을 표준 PPTX로 저장", ".pptx"),
    Action("office:xlsx_print_pdf", "PDF (Excel 인쇄)", "Excel에 저장된 인쇄 영역과 페이지 설정으로 PDF 출력", ".pdf"),
    Action("document:hwp", "HWP", "rhwp로 HWP 저장", ".hwp"),
    Action("document:hwpx", "HWPX", "rhwp/문서 모델로 표·그림·수식 변환", ".hwpx"),
    *(Action(f"sheet:{ext}", ext.upper(), f"표 데이터를 {ext.upper()}로 변환", f".{ext}")
      for ext in ("xlsx", "ods", "csv", "tsv")),
    *(Action(f"subtitle:{ext}", ext.upper(), f"자막을 {ext.upper()}로 변환", f".{ext}")
      for ext in ("srt", "vtt", "txt")),
    Action("archive:zip", "ZIP", "압축 내용을 ZIP으로 다시 묶기", ".zip"),
    Action("archive:tar", "TAR", "압축 내용을 TAR로 다시 묶기", ".tar"),
    Action("archive:gzip", "GZIP", "압축 내용을 tar.gz로 다시 묶기", ".tar.gz"),
    Action("strip", "메타데이터 제거", "지원 이미지·미디어의 메타데이터를 제거한 복사본"),
    *(Action(f"data:{ext}", ext.upper() + (" (자료)" if ext in {"csv", "tsv"} else ""), "자료형 보존용 magic-data XML 컨테이너 · $xml 문서 모델 복구" if ext == "xml" else "구조화 데이터 변환 · 형식별 자료형 제약 적용", f".{ext}")
      for ext in ("json", "jsonl", "yaml", "toml", "xml", "csv", "tsv")),
    *(Action(f"font:{ext}", ext.upper(), "폰트 컨테이너 변환 · 외곽선/문자 매핑/fsType 보존 · CFF→TTF 제외", f".{ext}")
      for ext in ("ttf", "otf", "woff", "woff2")),
    Action("ebook:fb2", "FB2", "장·기본 메타데이터·래스터 이미지 · 레이아웃 단순화", ".fb2"),
    Action("ebook:cbz", "CBZ", "PDF를 144 DPI 페이지 이미지 만화로 변환", ".cbz"),
    Action("email:attachments", "메일 보관 ZIP", "원본 EML·첨부 바이트·파일명/SHA-256 목록 보관", ".zip"),
    *(Action(f"mesh:{ext}", ext.upper(), "삼각형 메시 좌표·방향 변환 · 재질·텍스처·단위 제외", f".{ext}")
      for ext in ("obj", "stl", "ply")),
    *(Action(f"geo:{ext}", ext.upper(), "WGS84 경위도 변환 · GPX는 점·선만 지원", f".{ext}")
      for ext in ("geojson", "kml", "kmz", "gpx")),
)
ACTION_ACTIONS = (
    Action("copy_path", "경로 복사", "전체 경로를 줄바꿈으로 구분해 복사"),
    Action("strip", "메타데이터 제거", "지원 이미지·미디어의 메타데이터를 제거한 복사본"),
    Action("zip", "ZIP 압축", "선택 파일을 하나의 ZIP으로 묶기", ".zip"),
    Action("terminal", "터미널", "각 파일의 폴더에서 PowerShell 열기"),
)
TOOL_ACTIONS = tuple(Action(tool.key, tool.label, tool.description) for tool in TOOLS)
ALL_ACTIONS = {action.key: action for action in (*CONVERT_ACTIONS, *ACTION_ACTIONS, *TOOL_ACTIONS)}


@dataclass(frozen=True)
class Slot:
    action: Action
    enabled: bool
    reason: str = ""
    preset: Preset | None = None


def normalize_paths(paths: Iterable[str | Path]) -> tuple[Path, ...]:
    """Deduplicate paths in Explorer order without opening or reading files."""
    result: list[Path] = []
    seen: set[str] = set()
    for item in paths:
        path = Path(item).absolute()
        identity = str(path).casefold()
        if identity not in seen:
            seen.add(identity)
            result.append(path)
    return tuple(result)


def supports(action: Action, path: Path) -> bool:
    from magic_converter.core.conversion.builtin_converters import create_catalog
    return create_catalog().assess(path, action.key).route is not None


def slots_for(paths: Iterable[str | Path], mode: Mode, ffmpeg_available: bool = True,
              presentation_available: bool = True,
              rhwp_available: bool | None = None) -> tuple[Slot, ...]:
    if rhwp_available is None:
        from magic_converter.core.engines.rhwp_engine import locate
        rhwp_available = locate() is not None
    files = normalize_paths(paths)
    if mode == "action":
        actions = ACTION_ACTIONS
        if files:
            from magic_converter.core.conversion.builtin_converters import create_catalog
            potential = create_catalog()
            actions += tuple(action for action in TOOL_ACTIONS
                             if all(potential.assess(path, action.key).route is not None for path in files))
    elif not files:
        # Meaningful placeholders before Explorer supplies its MIME payload.
        actions = tuple(a for a in CONVERT_ACTIONS if a.key in {
            "image:png", "image:jpg", "media:mp4", "media:gif", "media:mp3", "media:webm"
        })
    elif all(p.suffix.lower() in IMAGE_EXTENSIONS for p in files):
        actions = tuple(a for a in CONVERT_ACTIONS if a.key.startswith("image:") or a.key in {"compress", "document:pdf"})
    elif all(p.suffix.lower() in IMAGE_EXTENSIONS | {".gif"} for p in files):
        actions = tuple(ALL_ACTIONS[key] for key in ("image:png", "image:webp"))
        if all(p.suffix.lower() == ".gif" for p in files):
            actions += tuple(a for a in CONVERT_ACTIONS if a.key.startswith("media:") or a.key == "strip")
    elif all(p.suffix.lower() in AUDIO_EXTENSIONS for p in files):
        actions = tuple(a for a in CONVERT_ACTIONS if a.key.startswith("audio:") or a.key == "media:mp3")
    elif all(p.suffix.lower() in MEDIA_EXTENSIONS for p in files):
        actions = tuple(a for a in CONVERT_ACTIONS if a.key.startswith("media:") or a.key == "strip")
    elif all(p.suffix.lower() == ".pdf" for p in files):
        actions = tuple(ALL_ACTIONS[key] for key in ("document:docx", "pptx_image", "pptx_editable", "document:xlsx", "document:jpg", "document:png", "document:epub", "document:txt", "document:md"))
    elif all(p.suffix.lower() in HWP_EXTENSIONS for p in files):
        targets = ["docx", "pdf"]
        if all(p.suffix.lower() == ".hwp" for p in files):
            targets.append("hwpx")
        elif all(p.suffix.lower() == ".hwpx" for p in files):
            targets.append("hwp")
        actions = tuple(ALL_ACTIONS[f"document:{ext}"] for ext in (*targets, "txt"))
    elif all(p.suffix.lower() in SLIDE_EXTENSIONS for p in files):
        actions = tuple(ALL_ACTIONS[f"document:{ext}"] for ext in ("pdf", "jpg", "png"))
        if all(p.suffix.lower() != ".ppt" for p in files):
            actions += (ALL_ACTIONS["office:pptx"],)
    elif all(p.suffix.lower() in DOCUMENT_EXTENSIONS for p in files):
        actions = tuple(ALL_ACTIONS[f"document:{ext}"] for ext in ("docx", "md", "txt", "epub", "html", "pdf"))
    elif all(p.suffix.lower() in SHEET_EXTENSIONS for p in files):
        actions = tuple(ALL_ACTIONS[f"sheet:{ext}"] for ext in ("xlsx", "ods", "csv", "tsv")) + (ALL_ACTIONS["document:pdf"],)
        if all(p.suffix.lower() == ".xlsx" for p in files):
            actions += (ALL_ACTIONS["office:xlsx_print_pdf"],)
    elif all(p.suffix.lower() in {".srt", ".vtt"} for p in files):
        actions = tuple(ALL_ACTIONS[f"subtitle:{ext}"] for ext in ("srt", "vtt", "txt"))
    elif all(p.suffix.lower() in ARCHIVE_EXTENSIONS for p in files):
        actions = tuple(ALL_ACTIONS[f"archive:{ext}"] for ext in ("zip", "tar", "gzip"))
    else:
        # Keep the established single-family layouts. For a mixed selection,
        # expose conversions every file can perform rather than only metadata.
        from magic_converter.core.conversion.builtin_converters import create_catalog
        potential = create_catalog()
        common = ("document:pdf", "document:docx", "document:txt", "document:md",
                  "document:epub", "document:html", "document:jpg", "document:png", "strip")
        actions = tuple(ALL_ACTIONS[key] for key in common
                        if all(potential.assess(path, key).route is not None for path in files))
        if not actions:
            actions = (ALL_ACTIONS["strip"],)

    from magic_converter.core.engines.office_formats import OFFICE
    from magic_converter.core.conversion.formats import FORMATS
    from magic_converter.core.specialized.special_formats import visible_keys
    if mode == "convert" and files:
        detected = [FORMATS.detect(p) for p in files]
        direct = set.intersection(*(set(visible_keys(f.id)) if f else set() for f in detected))
        if direct:
            # Dedicated families replace the generic fallback. CSV/TSV and
            # documents retain their established actions and gain new targets.
            if all(f and f.category in {"data", "font", "ebook", "email", "mesh", "geo"} or f and f.id == "epub" for f in detected):
                actions = ()
            present = {a.key for a in actions}
            actions += tuple(a for a in CONVERT_ACTIONS if a.key in direct and a.key not in present)
    if mode == "convert" and files and all(p.suffix.lower() in OFFICE for p in files):
        additions = ("hwp", "hwpx")
        actions += tuple(ALL_ACTIONS[f"document:{target}"] for target in additions)

    from magic_converter.core.conversion.builtin_converters import create_catalog
    catalog = create_catalog(ffmpeg_available=ffmpeg_available, presentation_available=presentation_available,
                             rhwp_available=rhwp_available)
    slots: list[Slot] = []
    for action in actions:
        reason = ""
        if not files:
            reason = "파일을 휠 위로 드래그하세요"
        else:
            reason = next((availability.reason for path in files
                           if (availability := catalog.assess(path, action.key)).route is None), "")
        slots.append(Slot(action, not reason, reason))
    return tuple(slots)


def describe_files(paths: Iterable[str | Path]) -> str:
    files = normalize_paths(paths)
    if not files:
        return "파일 대기 중"
    extensions = sorted({path.suffix[1:].upper() or "파일" for path in files})
    kinds = "/".join(extensions[:3]) + ("…" if len(extensions) > 3 else "")
    return f"{len(files)}개 · {kinds}"

