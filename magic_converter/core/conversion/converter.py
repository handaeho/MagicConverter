"""Background encoders and actions. Originals are never overwritten."""

from __future__ import annotations

import logging
import io
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from PIL import Image, ImageOps, ImageSequence
from pillow_heif import register_heif_opener
from PySide6.QtCore import QObject, QRunnable, Signal, Slot
import pymupdf as fitz
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.text import MSO_ANCHOR, MSO_AUTO_SIZE, PP_ALIGN
from pptx.oxml.xmlchemy import OxmlElement
from pptx.oxml.ns import qn
from pptx.util import Pt

from magic_converter.core.conversion.file_router import AUDIO_EXTENSIONS, IMAGE_EXTENSIONS, Action, normalize_paths
from magic_converter.core.jobs.job_control import Control, JobCancelled, JobTimedOut, check_cancelled, current, copy_stream, checked
from magic_converter.core.jobs.job_models import FileResult, JobProgress, JobResult
from magic_converter.core.conversion.conversion_request import ConversionRequest, prepare_request
from magic_converter.core.settings.preferences import EncoderOptions, OutputRule
from magic_converter.core.pdf.pdf_layout import (PYMUPDF_LOCK, PdfTextSpan, PdfTable, text_spans as _pdf_text_spans,
                         cell_alignment, cell_margins, lines as _pdf_lines)
from magic_converter.core.pdf.pdf_pptx_text import (add_positioned_runs, configure_paragraph, positioned_spans,
                            cell_vertical_layout, span_geometry, set_east_asian_font as _set_east_asian_font)
from magic_converter.core.pdf.pdf_pptx_objects import restore_page

LOGGER = logging.getLogger(__name__)
IMAGE_FORMATS = {".png": "PNG", ".jpg": "JPEG", ".jpeg": "JPEG", ".jpe": "JPEG", ".jfif": "JPEG", ".webp": "WEBP",
                 ".bmp": "BMP", ".tif": "TIFF", ".tiff": "TIFF", ".heic": "HEIF", ".heif": "HEIF", ".avif": "AVIF", ".pdf": "PDF"}
register_heif_opener(thumbnails=False)


class ConversionError(RuntimeError):
    """A user-readable encoder or input error."""


def _add_editable_span(slide: object, span: PdfTextSpan, scale: float, offset_x: float, offset_y: float,
                        canvas_width: float | None = None) -> None:
    for region in positioned_spans(span):
        _add_text_region(slide, region, scale, offset_x, offset_y)


def _add_text_region(slide, span, scale, offset_x, offset_y):
    left, top, width, height, rotation, leading, inset_top = span_geometry(span)
    textbox = slide.shapes.add_textbox(Pt(offset_x + left * scale), Pt(offset_y + top * scale),
                                       Pt(width * scale), Pt(height * scale))
    textbox.rotation = rotation
    textbox.fill.background()
    textbox.line.fill.background()
    frame = textbox.text_frame
    frame.word_wrap = False
    frame.auto_size = MSO_AUTO_SIZE.NONE
    frame.vertical_anchor = MSO_ANCHOR.TOP
    frame.margin_left = frame.margin_right = frame.margin_top = frame.margin_bottom = Pt(0)
    frame.margin_top = Pt(inset_top * scale)
    paragraph = frame.paragraphs[0]
    configure_paragraph(paragraph)
    paragraph.line_spacing = Pt(leading * scale)
    add_positioned_runs(paragraph, span, scale)


def _add_editable_table(slide, source: PdfTable, scale: float, offset_x: float, offset_y: float) -> tuple[int, int]:
    positioned_cells, textboxes = 0, 0
    shape = slide.shapes.add_table(source.rows, source.cols,
                                   Pt(offset_x + source.bbox[0] * scale), Pt(offset_y + source.bbox[1] * scale),
                                   Pt((source.bbox[2] - source.bbox[0]) * scale),
                                   Pt((source.bbox[3] - source.bbox[1]) * scale))
    table = shape.table
    table.first_row = table.first_col = table.last_row = table.last_col = False
    table.horz_banding = table.vert_banding = False
    for index, column in enumerate(table.columns):
        column.width = Pt((source.xs[index + 1] - source.xs[index]) * scale)
    for index, row in enumerate(table.rows):
        row.height = Pt((source.ys[index + 1] - source.ys[index]) * scale)
    for item in source.cells:
        check_cancelled()
        cell = table.cell(item.row, item.col)
        if item.rowspan > 1 or item.colspan > 1:
            cell.merge(table.cell(item.row + item.rowspan - 1, item.col + item.colspan - 1))
        cell.fill.solid()
        cell.fill.fore_color.rgb = RGBColor.from_string(item.fill)
        left, right, top, bottom = cell_margins(item)
        cell.margin_left, cell.margin_right = Pt(left * scale), Pt(right * scale)
        cell.margin_top, cell.margin_bottom = Pt(top * scale), Pt(bottom * scale)
        cell.vertical_anchor = MSO_ANCHOR.TOP
        properties = cell._tc.get_or_add_tcPr()
        for edge, side in reversed((("lnL", "left"), ("lnR", "right"), ("lnT", "top"), ("lnB", "bottom"))):
            previous = properties.find(qn("a:" + edge))
            if previous is not None:
                properties.remove(previous)
            line = OxmlElement("a:" + edge)
            border = (source.border_color, source.border_width) if item.borders is None else item.borders.get(side)
            if border is None:
                line.append(OxmlElement("a:noFill"))
            else:
                line.set("w", str(Pt(border[1] * scale)))
                fill = OxmlElement("a:solidFill")
                color = OxmlElement("a:srgbClr")
                color.set("val", border[0])
                fill.append(color)
                line.append(fill)
            properties.insert(0, line)
        frame = cell.text_frame
        frame.clear()
        frame.word_wrap, frame.auto_size = False, MSO_AUTO_SIZE.NONE
        cell_lines = _pdf_lines(item.spans)
        vertical = cell_vertical_layout(item, cell_lines)
        if not cell_lines or vertical is None:
            # Even an empty cell inherits a template's large end-paragraph
            # font. Office expands tight rows to that invisible minimum.
            empty = frame.paragraphs[0]
            empty.font.size = Pt(1)
            empty.line_spacing = Pt(1)
            empty.space_before = empty.space_after = Pt(0)
        if cell_lines and vertical is None:
            # Native cell paragraphs cannot express every stored PDF gap.
            # Preserve the grid/merges as a table and the tight cell's text
            # as independently editable, fixed-baseline regions. Do not
            # silently replace its leading with the 1.15-font-size guess.
            positioned_cells += 1
            for span in item.spans:
                first = len(slide.shapes)
                _add_editable_span(slide, span, scale, offset_x, offset_y)
                for index in range(first, len(slide.shapes)):
                    slide.shapes[index].name = f"PDF cell R{item.row + 1}C{item.col + 1} text region {textboxes + index - first + 1}"
                textboxes += len(slide.shapes) - first
            continue
        if vertical is not None:
            cell.margin_top = Pt(vertical[0] * scale)
        for index, spans in enumerate(cell_lines):
            paragraph = frame.paragraphs[0] if index == 0 else frame.add_paragraph()
            configure_paragraph(paragraph)
            paragraph.line_spacing = Pt((vertical[1][index] if vertical is not None
                                         else max(span.size for span in spans) * 1.15) * scale)
            if vertical is not None:
                paragraph.space_after = Pt(vertical[2][index] * scale)
            paragraph.alignment = {"left": PP_ALIGN.LEFT, "center": PP_ALIGN.CENTER,
                                   "right": PP_ALIGN.RIGHT}[cell_alignment(item)]
            previous_span = None
            baseline = max(spans, key=lambda s: s.size).origin[1]
            for span in spans:
                if (previous_span is not None and previous_span.text and span.text
                        and not previous_span.text[-1].isspace() and not span.text[0].isspace()
                        and span.bbox[0] - previous_span.bbox[2] > min(span.size, previous_span.size) * .18):
                    space = paragraph.add_run()
                    space.text = " "
                    space.font.size = Pt(max(span.size, previous_span.size) * scale)
                first_run = len(paragraph.runs)
                add_positioned_runs(paragraph, span, scale)
                shift = baseline - span.origin[1]
                if abs(shift) > .5 and span.size < max(s.size for s in spans) * .9:
                    for run in paragraph.runs[first_run:]:
                        run._r.get_or_add_rPr().set("baseline", str(round(shift / span.size * 100000)))
                previous_span = span
    for item in source.cells:
        for picture in item.pictures:
            bounds = picture.bbox
            slide.shapes.add_picture(io.BytesIO(picture.png), Pt(offset_x + bounds[0] * scale),
                                     Pt(offset_y + bounds[1] * scale), width=Pt((bounds[2] - bounds[0]) * scale),
                                     height=Pt((bounds[3] - bounds[1]) * scale))
    return positioned_cells, textboxes


def _convert_pdf_pptx(source: Path, destination: Path, *, editable: bool,
                       progress: Callable[[int | None], None] | None = None,
                       warning: Callable[[str], None] = lambda _: None) -> None:
    """Restore PDF objects or render fixed slides, without changing the source.

    PowerPoint has one canvas size for the whole deck. The first PDF page
    defines it; mixed-size pages fit proportionally with white margins.
    Same-size PDFs use exact 1 PDF point == 1 PowerPoint point coordinates.
    """
    with PYMUPDF_LOCK, fitz.open(str(source)) as document:
        if document.needs_pass:
            raise ConversionError("암호로 보호된 PDF는 잠금을 해제한 뒤 변환하세요.")
        if not document.page_count:
            raise ConversionError("PDF에 페이지가 없습니다.")
        presentation = Presentation()
        canvas = document[0].rect
        try:
            presentation.slide_width = Pt(canvas.width)
            presentation.slide_height = Pt(canvas.height)
        except ValueError as exc:
            raise ConversionError("첫 페이지 크기가 PowerPoint의 슬라이드 크기 범위를 벗어납니다.") from exc
        for index, page in enumerate(document):
            check_cancelled()
            if editable and page.rotation:
                page.remove_rotation()
            rect = page.rect
            scale = min(canvas.width / rect.width, canvas.height / rect.height)
            offset_x, offset_y = (canvas.width - rect.width * scale) / 2, (canvas.height - rect.height * scale) / 2
            if editable:
                slide = presentation.slides.add_slide(presentation.slide_layouts[6])
                slide.background.fill.solid()
                slide.background.fill.fore_color.rgb = RGBColor(255, 255, 255)
                # This converter owns one Slide/SlideShapes proxy per page.
                # Avoid a document-wide ID scan for every PDF path. Group
                # emission refreshes this cache after allocating child IDs.
                shape_tree = slide.shapes
                shape_tree.turbo_add_enabled = True
                try:
                    restore_page(page, slide, scale, offset_x, offset_y, _add_editable_span,
                                 _add_editable_table, warning, index + 1)
                finally:
                    shape_tree.turbo_add_enabled = False
                if progress:
                    progress(min(99, (index + 1) * 100 // document.page_count))
                continue
            pixmap = page.get_pixmap(dpi=200, alpha=False)
            image = io.BytesIO(pixmap.tobytes("png"))
            del pixmap
            slide = presentation.slides.add_slide(presentation.slide_layouts[6])
            slide.background.fill.solid()
            slide.background.fill.fore_color.rgb = RGBColor(255, 255, 255)
            slide.shapes.add_picture(image, Pt(offset_x), Pt(offset_y),
                                     width=Pt(rect.width * scale), height=Pt(rect.height * scale))
            if progress:
                progress(min(99, (index + 1) * 100 // document.page_count))
        presentation.save(destination)
    if progress:
        progress(100)


def pdf_to_pptx_image(source: Path, destination: Path, progress: Callable[[int | None], None] | None = None) -> None:
    """One full-page 200 DPI image per slide."""
    _convert_pdf_pptx(source, destination, editable=False, progress=progress)


def pdf_to_pptx_editable(source: Path, destination: Path, progress: Callable[[int | None], None] | None = None,
                         warning: Callable[[str], None] = lambda _: None) -> None:
    """Native PDF paths, positioned text, tables and individual pictures."""
    _convert_pdf_pptx(source, destination, editable=True, progress=progress, warning=warning)


def find_ffmpeg(configured: str = "") -> str | None:
    candidate = configured.strip() or os.environ.get("MAGIC_CONVERTER_FFMPEG", "").strip()
    if candidate:
        path = Path(candidate).expanduser()
        resolved = str(path.absolute()) if path.is_file() else shutil.which(candidate)
        if resolved:
            return resolved
    bundled = bundled_tool("ffmpeg")
    return str(bundled) if bundled else shutil.which("ffmpeg")


def bundled_tool(name: str) -> Path | None:
    root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
    candidate = root / "vendor" / "ffmpeg" / f"{name}.exe"
    return candidate if candidate.is_file() else None


def find_ffprobe(ffmpeg: str) -> str | None:
    sibling = Path(ffmpeg).with_name("ffprobe.exe" if os.name == "nt" else "ffprobe")
    if sibling.is_file():
        return str(sibling)
    bundled = bundled_tool("ffprobe")
    return str(bundled) if bundled else shutil.which("ffprobe")


def probe_duration(source: Path, ffmpeg: str, *, streams: list[dict] | None = None) -> float | None:
    """Probe on the worker thread. Unknown/live durations stay indeterminate."""
    ffprobe = find_ffprobe(ffmpeg)
    if not ffprobe:
        return None
    try:
        from magic_converter.core.jobs.process_runner import run_process
        entries = "format=duration:stream=duration"
        if streams is not None:
            entries += ",codec_type,bits_per_sample,bits_per_raw_sample:stream_disposition=attached_pic"
        result = run_process([ffprobe, "-v", "error", "-show_entries", entries, "-of", "json", str(source)], timeout=30)
        if result.returncode:
            return None
        data = json.loads(result.stdout)
        if streams is not None:
            streams.extend(data.get("streams", ()))
        durations = [data.get("format", {}).get("duration")]
        durations += [stream.get("duration") for stream in data.get("streams", [])]
        valid: list[float] = []
        for duration in durations:
            try:
                number = float(duration)
            except (TypeError, ValueError):
                continue
            if math.isfinite(number) and number > 0:
                valid.append(number)
        return max(valid) if valid else None
    except (JobCancelled, JobTimedOut):
        raise
    except (OSError, ValueError, subprocess.SubprocessError):
        LOGGER.info("Duration unavailable: %s", source, exc_info=True)
        return None


class FFmpegProgressParser:
    """Parse key=value blocks without interpreting out_time_ms as milliseconds."""
    def __init__(self, duration: float | None) -> None:
        self.duration = duration
        self.percent = -1

    def feed(self, line: str) -> int | None:
        key, separator, value = line.strip().partition("=")
        if not separator or not self.duration:
            return None
        try:
            if key in {"out_time_us", "out_time_ms"}:
                seconds = int(value) / 1_000_000
            elif key == "out_time":
                hours, minutes, seconds_text = value.split(":")
                seconds = int(hours) * 3600 + int(minutes) * 60 + float(seconds_text)
            else:
                return None
            if not math.isfinite(seconds) or seconds < 0:
                return None
            # 100% is emitted only after FFmpeg exits successfully. In
            # particular, progress=end itself is not proof of success.
            percent = max(self.percent, min(99, int(seconds / self.duration * 100)))
        except (TypeError, ValueError, OverflowError):
            return None
        if percent == self.percent:
            return None
        self.percent = percent
        return percent


@contextmanager
def output_file(source: Path, suffix: str, tag: str = "converted", *,
                rule: OutputRule | None = None, preset_name: str = "",
                allow_empty: bool = False) -> Iterator[tuple[Path, Path]]:
    """Reserve the destination atomically, encode to a sibling temporary file.

    Exclusive creation also handles concurrent drops targeting the same name.
    Only a fully encoded file replaces our reservation; errors remove both.
    """
    check_cancelled()
    root, basename = (rule or OutputRule()).destination(source, tag, preset_name)
    root.mkdir(parents=True, exist_ok=True)
    index = 0
    while True:
        numbered = f"({index})" if index else ""
        destination = root / f"{basename}{numbered}{suffix}"
        try:
            reservation = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o666)
            os.close(reservation)
            break
        except FileExistsError:
            if rule is not None and rule.collision == "error":
                raise ConversionError(f"같은 이름의 출력이 이미 있습니다: {destination}") from None
            index += 1
    temporary: Path | None = None
    try:
        fd, name = tempfile.mkstemp(prefix=".magic_converter-", suffix=suffix, dir=root)
        os.close(fd)
        temporary = Path(name)
        yield temporary, destination
        check_cancelled()
        if not allow_empty and temporary.stat().st_size == 0:
            raise ConversionError("변환 결과가 비어 있습니다.")
        publication_deadline = time.monotonic() + 1.0
        while True:
            check_cancelled()
            try:
                os.replace(temporary, destination)
                break
            except PermissionError as error:
                # Windows scanners/exporters may briefly retain a handle
                # without FILE_SHARE_DELETE after writing a finished result.
                # Keep the atomic reservation; never fall back to a copy or
                # extend the job's cancellation/deadline while waiting.
                if (os.name != "nt" or getattr(error, "winerror", None) not in {5, 32, 33}
                        or time.monotonic() >= publication_deadline):
                    raise
                time.sleep(.05)
        temporary = None
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def _load_frames(source: Path, *, preserve_timing: bool = False,
                 notice: Callable[[str], None] = lambda _: None) -> tuple[list[Image.Image], list[int | float], int]:
    frames: list[Image.Image] = []
    durations: list[int] = []
    if source.suffix.lower() == ".svg":
        from PySide6.QtCore import QBuffer, QIODevice
        from PySide6.QtGui import QImage, QPainter
        from PySide6.QtSvg import QSvgRenderer
        renderer = QSvgRenderer(str(source))
        if not renderer.isValid():
            raise ConversionError("유효한 SVG 파일이 아닙니다.")
        size = renderer.defaultSize()
        if size.isEmpty() or size.width() * size.height() > 80_000_000:
            raise ConversionError("SVG 크기가 없거나 너무 큽니다.")
        image = QImage(size, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(0)
        painter = QPainter(image)
        renderer.render(painter)
        painter.end()
        buffer = QBuffer()
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        image.save(buffer, "PNG")
        with Image.open(io.BytesIO(bytes(buffer.data()))) as decoded:
            return [decoded.convert("RGBA")], [100], 0
    try:
        with Image.open(source) as image:
            loop = int(image.info.get("loop", 0))
            metadata = False
            high_depth = False
            for frame in checked(ImageSequence.Iterator(image)):
                metadata |= any(key in frame.info for key in ("icc_profile", "exif", "xmp", "dpi"))
                high_depth |= _high_depth_image(frame)
                duration = frame.info.get("duration", image.info.get("duration", 100))
                durations.append(duration if preserve_timing else int(duration or 100))
                normalized = ImageOps.exif_transpose(frame).convert("RGBA")
                # Copy pixels into an entirely new image: EXIF/XMP/ICC/text and
                # TIFF tags cannot leak through Pillow's image.info defaults.
                clean = Image.new("RGBA", normalized.size)
                clean.paste(normalized)
                frames.append(clean)
            if metadata:
                notice("이 이미지 출력 경로는 원본의 색상 프로필·해상도·EXIF/XMP 메타데이터를 보존하지 않습니다.")
            if high_depth:
                notice("이 이미지 출력 경로는 원본의 높은 비트 깊이를 8비트 픽셀로 변환합니다.")
        return frames, durations, loop
    except Exception:
        for frame in frames:
            frame.close()
        raise


def _save_frames(frames: list[Image.Image], destination: Path, suffix: str,
                 durations: list[int], loop: int, quality: int = 90, *, lossless: bool = False) -> None:
    fmt = IMAGE_FORMATS[suffix]
    if len(frames) > 1 and fmt not in {"PNG", "WEBP", "TIFF", "HEIF", "AVIF", "PDF"}:
        raise ConversionError("다중 프레임은 PNG/WebP/TIFF/HEIC/AVIF/PDF로 변환하세요.")
    prepared: list[Image.Image] = []
    try:
        for frame in checked(frames):
            if fmt in {"JPEG", "BMP", "PDF"}:
                canvas = Image.new("RGB", frame.size, "white")
                canvas.paste(frame, mask=frame.getchannel("A"))
            else:
                canvas = frame.copy()
            canvas.info.clear()
            prepared.append(canvas)
        options: dict = {}
        if fmt == "JPEG":
            options.update(quality=quality, optimize=True, progressive=True)
        elif fmt == "PNG":
            options.update(optimize=True, compress_level=9)
        elif fmt == "WEBP":
            options.update(quality=quality, method=6, lossless=lossless)
            if lossless:
                # Full keyframes avoid libwebp delta-frame alpha loss on small
                # transparent GIF canvases; validated against decoded frames.
                options.update(kmin=1, kmax=1)
        elif fmt == "TIFF":
            options.update(compression="tiff_adobe_deflate")
        elif fmt in {"HEIF", "AVIF"}:
            options.update(quality=quality)
        elif fmt == "PDF":
            options.update(resolution=300.0)
        if len(prepared) > 1:
            options.update(save_all=True, append_images=prepared[1:])
            if fmt in {"PNG", "WEBP"}:
                options.update(duration=durations, loop=loop)
        prepared[0].save(destination, format=fmt, **options)
    finally:
        for image in prepared:
            image.close()


from magic_converter.core.jobs.raw_api import bounded_raw


PNG_NATIVE_MODES = frozenset({"1", "L", "LA", "P", "RGB", "RGBA", "I;16", "I;16B"})


def _high_depth_image(image: Image.Image) -> bool:
    if image.mode in {"I", "F"} or image.mode.startswith("I;16"):
        return True
    raw_mode = getattr(getattr(image, "png", None), "im_rawmode", "") or ""
    bits = getattr(image, "tag_v2", {}).get(258, ())
    if isinstance(bits, int):
        bits = (bits,)
    return (";16" in raw_mode or max(bits, default=0) > 8
            or int(image.info.get("bit_depth") or 8) > 8)


def _write_native_png(source: Path, destination: Path, max_edge: int, notice: Callable[[str], None]) -> bool:
    with source.open("rb") as stream, Image.open(stream) as image:
        if image.format == "PNG" and (not max_edge or max(image.size) <= max_edge):
            image.verify()
            stream.seek(0)
            with destination.open("wb") as output:
                copy_stream(stream, output)
            return True
        if getattr(image, "n_frames", 1) != 1:
            return False
        if _high_depth_image(image) and image.mode not in {"I;16", "I;16B"}:
            raise ConversionError("이 PNG 변환 경로는 원본의 높은 비트 깊이를 보존할 수 없습니다.")
        if image.mode not in PNG_NATIVE_MODES:
            return False
        orientation = image.getexif().get(274)
        ImageOps.exif_transpose(image, in_place=True)
        options = {key: image.info[key] for key in ("icc_profile", "dpi", "exif", "transparency")
                   if image.info.get(key) is not None}
        if orientation in {5, 6, 7, 8} and "dpi" in options:
            options["dpi"] = options["dpi"][::-1]
        if max_edge:
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        if image.info.get("xmp") or image.info.get("XML:com.adobe.xmp"):
            notice("이 PNG 인코딩 경로는 원본 XMP 메타데이터를 보존하지 않습니다.")
        check_cancelled()
        image.save(destination, format="PNG", optimize=True, compress_level=9, **options)
        return True


@bounded_raw
def convert_image(source: Path, destination: Path, suffix: str, *, compress: bool = False,
                  quality: int = 90, max_edge: int = 0, strip_metadata: bool = False,
                  notice: Callable[[str], None] = lambda _: None) -> None:
    if (suffix == ".png" and not compress and not strip_metadata and source.suffix.lower() != ".svg"
            and _write_native_png(source, destination, max_edge, notice)):
        return
    gif = source.suffix.lower() == ".gif"
    frames, durations, loop = _load_frames(source, preserve_timing=gif,
                                          notice=(lambda _: None) if strip_metadata else notice)
    if gif:
        # GIF stores repeat count; APNG/WebP store total play count (0=infinite).
        with Image.open(source) as original:
            repeats = original.info.get("loop")
        loop = 1 if repeats is None else (repeats + 1 if repeats else 0)
    try:
        if max_edge:
            for frame in checked(frames):
                frame.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
        if not compress:
            _save_frames(frames, destination, suffix, durations, loop, quality, lossless=gif)
            return
        target_size = source.stat().st_size // 2
        quality_steps = (88, 72, 55, 38, 22) if IMAGE_FORMATS[suffix] in {"JPEG", "WEBP", "HEIF", "AVIF"} else (90,)
        width, height = frames[0].size
        while True:
            check_cancelled()
            scaled = [frame.resize((max(1, round(frame.width * width / frames[0].width)),
                                    max(1, round(frame.height * height / frames[0].height))),
                                   Image.Resampling.LANCZOS) for frame in checked(frames)]
            try:
                for quality in checked(quality_steps):
                    _save_frames(scaled, destination, suffix, durations, loop, quality)
                    if destination.stat().st_size <= target_size:
                        return
            finally:
                for frame in scaled:
                    frame.close()
            if width == 1 and height == 1:
                raise ConversionError("포맷의 최소 파일 크기 때문에 50% 이하로 줄일 수 없습니다.")
            ratio = min(0.85, math.sqrt(target_size / max(1, destination.stat().st_size)) * 0.9)
            width, height = max(1, int(width * ratio)), max(1, int(height * ratio))
    finally:
        for frame in frames:
            frame.close()


def media_command(ffmpeg: str, source: Path, destination: Path, key: str) -> list[str]:
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
               "-nostats", "-stats_period", "0.2", "-progress", "pipe:1"]
    video_output = key in {"media:mp4", "media:mov", "media:mkv", "media:webm", "media:avi", "media:wmv"}
    if video_output and source.suffix.lower() in AUDIO_EXTENSIONS:
        command += ["-f", "lavfi", "-i", "color=c=0x1e1e23:s=640x360:r=24", "-i", str(source)]
        command += ["-map", "0:v:0", "-map", "1:a:0", "-shortest"]
    else:
        command += ["-i", str(source)]
        if video_output:
            command += ["-map", "0:v:0", "-map", "0:a:0?"]
    if key in {"media:mp4", "media:mov", "media:mkv"}:
        command += ["-vf", "scale=ceil(iw/2)*2:ceil(ih/2)*2", "-c:v", "libx264", "-preset", "medium",
                    "-crf", "20", "-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "192k",
                    ]
        if key != "media:mkv":
            command += ["-movflags", "+faststart"]
    elif key in {"media:avi", "media:wmv"}:
        command += ["-vf", "scale=ceil(iw/2)*2:ceil(ih/2)*2",
                    "-c:v", "mpeg4" if key.endswith("avi") else "wmv2", "-q:v", "4", "-pix_fmt", "yuv420p",
                    "-c:a", "libmp3lame" if key.endswith("avi") else "wmav2", "-b:a", "128k"]
    elif key == "media:webm":
        command += ["-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0", "-pix_fmt", "yuv420p",
                    "-c:a", "libopus", "-b:a", "128k"]
    elif key == "media:mp3":
        command += ["-map", "0:a:0", "-vn", "-c:a", "libmp3lame", "-q:a", "2"]
    elif key.startswith("audio:"):
        target = key.split(":")[1]
        codec = {"m4a": "aac", "wav": "pcm_s16le", "flac": "flac", "ogg": "libvorbis",
                 "opus": "libopus", "aiff": "pcm_s16be", "wma": "wmav2"}[target]
        command += ["-map", "0:a:0", "-vn", "-c:a", codec]
        if target in {"m4a", "opus", "wma"}:
            command += ["-b:a", "128k"]
        elif target == "ogg":
            command += ["-q:a", "5"]
    elif key == "media:gif":
        command += ["-filter_complex",
                    "[0:v:0]fps=15,scale=w='min(960,iw)':h=-1:flags=lanczos,split[a][b];"
                    "[a]palettegen=stats_mode=diff[p];[b][p]paletteuse=dither=sierra2_4a[v]",
                    "-map", "[v]", "-an", "-loop", "0"]
    elif key == "strip":
        command += ["-map", "0", "-c", "copy", "-map_metadata", "-1", "-map_metadata:s", "-1",
                    "-map_chapters", "-1", "-metadata", "encoder=", "-metadata:s", "encoder="]
        if source.suffix.lower() == ".mp3":
            command += ["-id3v2_version", "0", "-write_id3v1", "0"]
    else:
        raise ConversionError(f"알 수 없는 미디어 작업: {key}")
    command += ["-threads", "2", str(destination)]
    return command


def publish_folder(source: Path, producer: Callable[[Path], None], *,
                   rule: OutputRule | None = None, preset_name: str = "", allow_empty: bool = False) -> Path:
    """Stage all pages/sheets first, then publish a new collision-free folder."""
    check_cancelled()
    root, basename = (rule or OutputRule()).destination(source, "converted", preset_name)
    root.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".magic_converter-", dir=root) as directory:
        staging = Path(directory)
        producer(staging)
        check_cancelled()
        if not allow_empty and not any(staging.iterdir()):
            raise ConversionError("출력할 페이지 또는 시트가 없습니다.")
        index = 0
        while True:
            ending = f"({index})" if index else ""
            destination = root / f"{basename}{ending}"
            try:
                os.rename(staging, destination)
                return destination
            except (FileExistsError, PermissionError):
                if not destination.exists():
                    raise
                if rule is not None and rule.collision == "error":
                    raise ConversionError(f"같은 이름의 출력이 이미 있습니다: {destination}") from None
                index += 1


def _media_loss_notice(key: str, streams: list[dict], notice: Callable[[str], None]) -> None:
    if key == "strip":
        return
    audio = [stream for stream in streams if stream.get("codec_type") == "audio"]
    video = [stream for stream in streams if stream.get("codec_type") == "video"
             and not stream.get("disposition", {}).get("attached_pic")]
    audio_output = key == "media:mp3" or key.startswith("audio:")
    if key == "media:gif":
        notice("GIF에는 영상 한 트랙을 최대 폭 960·15fps·팔레트 색상으로 저장하며 음성·자막을 제외합니다.")
    elif audio_output:
        if len(audio) > 1:
            notice(f"오디오 출력은 첫 음성 트랙만 사용합니다. 나머지 {len(audio) - 1}개 음성 트랙은 제외합니다.")
        if key in {"audio:wav", "audio:aiff"} and audio:
            if any(str(audio[0].get(field, "")).isdigit() and int(audio[0][field]) > 16
                   for field in ("bits_per_sample", "bits_per_raw_sample")):
                notice("이 출력은 원본 음성의 비트 깊이를 16비트 PCM으로 줄입니다.")
    elif len(audio) > 1 or len(video) > 1 or len(audio) + len(video) != len(streams):
        notice("영상 재인코딩은 영상·음성의 첫 트랙만 사용하며 추가 트랙·자막·첨부를 제외합니다.")


def convert_media(source: Path, destination: Path, key: str, ffmpeg: str | None,
                  progress: Callable[[int | None], None] | None = None, *,
                  options: EncoderOptions = EncoderOptions(),
                  notice: Callable[[str], None] = lambda _: None) -> None:
    if not ffmpeg:
        raise ConversionError("FFmpeg를 찾지 못했습니다. PATH 또는 설정의 경로를 확인하세요.")
    if progress:
        progress(None)
    streams: list[dict] = []
    duration = probe_duration(source, ffmpeg, streams=streams)
    parser = FFmpegProgressParser(duration)
    if progress and duration:
        progress(0)
    from magic_converter.core.jobs.process_runner import run_process

    def update(line: str) -> None:
        percent = parser.feed(line)
        if percent is not None and progress:
            progress(percent)

    command = media_command(ffmpeg, source, destination, key)
    if options.video_crf is not None:
        command[command.index("-crf") + 1] = str(options.video_crf)
    if options.audio_bitrate is not None:
        if "-q:a" in command:
            index = command.index("-q:a")
            command[index:index + 2] = ["-b:a", f"{options.audio_bitrate}k"]
        elif "-b:a" in command:
            command[command.index("-b:a") + 1] = f"{options.audio_bitrate}k"
        else:
            command[-1:-1] = ["-b:a", f"{options.audio_bitrate}k"]
    result = run_process(command, idle_timeout=600, on_output=update)
    if result.returncode:
        raise ConversionError(result.stderr[-2400:].strip() or f"FFmpeg 종료 코드: {result.returncode}")
    _media_loss_notice(key, streams, notice)
    if progress:
        progress(100)


def create_zip(paths: tuple[Path, ...], *, rule: OutputRule | None = None, preset_name: str = "") -> Path:
    from magic_converter.core.jobs.combined_worker import local_encoding, execute
    if not local_encoding.get():
        from magic_converter.core.conversion.conversion_catalog import Route
        from magic_converter.core.conversion.conversion_request import PlannedFile
        from magic_converter.core.conversion.file_router import ALL_ACTIONS
        request = ConversionRequest(ALL_ACTIONS['zip'], tuple(PlannedFile(p, Route((), 0)) for p in paths), (),
                                    output=rule or OutputRule(), preset_name=preset_name)
        return execute(request, lambda _: None, lambda _: None)
    used: set[str] = set()
    with output_file(paths[0], ".zip", rule=rule, preset_name=preset_name) as (temporary, destination):
        with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6, allowZip64=True) as archive:
            def add_file(path: Path, name: str) -> None:
                check_cancelled()
                info = zipfile.ZipInfo.from_file(path, arcname=name)
                info.compress_type = zipfile.ZIP_DEFLATED
                with path.open("rb") as incoming, archive.open(info, "w", force_zip64=True) as outgoing:
                    copy_stream(incoming, outgoing)

            for path in checked(paths):
                name, index = path.name, 1
                while name.casefold() in used:
                    name = f"{path.stem}({index}){path.suffix}"
                    index += 1
                used.add(name.casefold())
                if path.is_dir():
                    archive.write(path, arcname=name)
                    for member in checked(sorted(path.rglob("*"))):
                        if member.is_symlink():
                            raise ConversionError("RTFD 번들의 심볼릭 링크는 압축하지 않습니다.")
                        member_name = str(Path(name) / member.relative_to(path))
                        if member.is_dir():
                            archive.write(member, arcname=member_name)
                        else:
                            add_file(member, member_name)
                else:
                    add_file(path, name)
    return destination


def open_terminals(paths: tuple[Path, ...]) -> None:
    if os.name != "nt":
        raise ConversionError("PowerShell 액션은 Windows에서만 지원합니다.")
    executable = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
    if not executable:
        raise ConversionError("PowerShell 실행 파일을 찾지 못했습니다.")
    parents = dict.fromkeys(path.parent for path in paths)
    for parent in parents:
        # No path is embedded in PowerShell code; shell metacharacters in file
        # names cannot become executable commands. The visible console is the
        # requested terminal, whereas FFmpeg always uses CREATE_NO_WINDOW.
        subprocess.Popen([executable, "-NoLogo", "-NoProfile", "-NoExit"], cwd=parent,
                         creationflags=subprocess.CREATE_NEW_CONSOLE, close_fds=True)


class WorkerSignals(QObject):
    finished = Signal(object)
    progress = Signal(object)


class ConversionWorker(QRunnable):
    def __init__(self, job_id: int, paths: tuple[Path, ...], action: Action, ffmpeg: str | None, *,
                 request: ConversionRequest | None = None, timeout: float | None = 3600) -> None:
        super().__init__()
        if timeout is not None and timeout <= 0:
            raise ValueError("timeout must be positive")
        self.timeout = timeout
        self.job_id, self.ffmpeg = job_id, ffmpeg
        self.paths = request.paths if request is not None else normalize_paths(paths)
        self.action = request.action if request is not None else action
        self.signals = WorkerSignals()
        self.cancel_event = threading.Event()
        self._file_index = 1
        self.request = request

    @Slot()
    def run(self) -> None:
        result = JobResult(self.job_id, self.action, len(self.paths))
        control = Control(self.cancel_event, lambda phase: self._progress(self._file_index, None, phase))
        token = current.set(control)
        com_initialized = False
        try:
            check_cancelled()
            from magic_converter.core.conversion.builtin_converters import runtime_catalog
            from magic_converter.core.conversion.conversion_execution import execute_file
            request = self.request or prepare_request(self.paths, self.action.key, runtime_catalog(self.ffmpeg))
            if os.name == "nt":
                import pythoncom
                pythoncom.CoInitialize()
                com_initialized = True
            def validate(path: Path) -> None:
                check_cancelled()
                if any(item.placeholder == path for item in request.web_inputs):
                    return
                if not path.is_file() and not (path.is_dir() and path.suffix.lower() == ".rtfd"):
                    raise ConversionError(f"일반 파일이 아니거나 접근할 수 없습니다: {path}")

            if request.combined:
                control.deadline = None if self.timeout is None else time.monotonic() + self.timeout
                for path in self.paths:
                    validate(path)
            if self.action.key in {"tool:pdf_merge", "zip"}:
                from magic_converter.core.jobs.combined_worker import execute
                self._progress(1, None, "PDF 병합 중" if self.action.key == "tool:pdf_merge" else "ZIP 압축 중")
                result.outputs.append(execute(request, lambda percent: self._progress(1, percent), result.warnings.append))
                result.completed = len(self.paths)
            elif self.action.key == "terminal":
                open_terminals(self.paths)
                result.completed = len(self.paths)
            else:
                for index, source in enumerate(self.paths, start=1):
                    self._file_index = index
                    control.deadline = None if self.timeout is None else time.monotonic() + self.timeout
                    first_warning = len(result.warnings)
                    try:
                        validate(source)
                        self._progress(index, None, "파일 분석 중")
                        callback = lambda percent: self._progress(index, min(99, percent) if percent is not None else None)
                        destination = execute_file(request, request.files[index - 1], self.ffmpeg,
                                                   callback, result.warnings.append)
                        result.outputs.append(destination)
                        result.completed += 1
                        result.files.append(FileResult(source, "done", (destination,),
                                                       warnings=tuple(result.warnings[first_warning:])))
                        self._progress(index, 100)
                    except JobCancelled:
                        raise
                    except JobTimedOut as exc:
                        result.timed_out = True
                        message = f"{source.name}: {exc}"
                        result.errors.append(message)
                        result.files.append(FileResult(source, "timed_out", errors=(message,),
                                                       warnings=tuple(result.warnings[first_warning:])))
                    except Exception as exc:
                        LOGGER.exception("Conversion failed: %s", source)
                        result.errors.append(f"{source.name}: {exc}")
                        result.files.append(FileResult(source, "failed", errors=(result.errors[-1],),
                                                       warnings=tuple(result.warnings[first_warning:])))
        except JobCancelled:
            result.cancelled = True
        except JobTimedOut as exc:
            result.timed_out = True
            result.errors.append(str(exc))
        except Exception as exc:
            LOGGER.exception("Batch action failed")
            result.errors.append(str(exc))
        finally:
            for source in self.paths[len(result.files):]:
                state = "cancelled" if result.cancelled else "timed_out" if result.timed_out else "failed" if result.errors else "done"
                result.files.append(FileResult(source, state, tuple(result.outputs) if state == "done" else (),
                                               tuple(result.errors), tuple(result.warnings)))
            if com_initialized:
                pythoncom.CoUninitialize()
            current.reset(token)
            self.signals.finished.emit(result)

    def _progress(self, index: int, percent: int | None, phase: str = "변환 중") -> None:
        check_cancelled()
        self.signals.progress.emit(JobProgress(self.job_id, self.action.label, self.paths[index - 1].name,
                                               index, len(self.paths), percent, phase))


from magic_converter.core.conversion.conversion_service import ConversionService  # Public compatibility for existing imports.
