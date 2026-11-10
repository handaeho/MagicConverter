"""PDF page operations retain PDF objects; originals are never rewritten."""

from contextlib import ExitStack
from pathlib import Path
import re
import tempfile

from pypdf import PdfReader, PdfWriter

from magic_converter.core.conversion.converter import ConversionError, convert_image, output_file
from magic_converter.core.jobs.job_control import check_cancelled, checked, copy_file


def selected_pages(spec: str, count: int) -> list[int]:
    if count < 1:
        raise ConversionError("PDF에 페이지가 없습니다.")
    if not spec.strip():
        return list(range(count))
    result, seen = [], set()
    for part in spec.split(","):
        match = re.fullmatch(r"\s*([1-9]\d*)\s*(?:-\s*([1-9]\d*)\s*)?", part)
        if match is None:
            raise ConversionError("PDF 페이지는 1-3,5처럼 지정하세요.")
        first, last = int(match[1]), int(match[2] or match[1])
        if first > count or last > count:
            raise ConversionError(f"PDF 페이지 범위가 전체 {count}페이지를 벗어났습니다.")
        for page in range(first, last + (1 if last >= first else -1), 1 if last >= first else -1):
            if page not in seen:
                seen.add(page)
                result.append(page - 1)
    return result


def _reader(stream):
    reader = PdfReader(stream)
    if reader.is_encrypted:
        raise ConversionError("암호로 보호된 PDF는 잠금을 해제한 뒤 사용하세요.")
    if not reader.pages:
        raise ConversionError("PDF에 페이지가 없습니다.")
    return reader


def merge_pdf(request, progress) -> Path:
    from magic_converter.core.jobs.combined_worker import local_encoding, execute
    if not local_encoding.get():
        return execute(request, progress, lambda _: None)
    from magic_converter.core.conversion.formats import FORMATS
    with output_file(request.paths[0], ".pdf", rule=request.output, preset_name=request.preset_name) as (temporary, destination):
        with tempfile.TemporaryDirectory(prefix="magic-pdf-merge-") as directory, ExitStack() as stack:
            writer = PdfWriter()
            try:
                for index, source in enumerate(checked(request.paths)):
                    path = source
                    if FORMATS.detect(source).id != "pdf":
                        path = Path(directory) / f"{index}.pdf"
                        convert_image(source, path, ".pdf")
                    reader = _reader(stack.enter_context(path.open("rb")))
                    for page in checked(reader.pages):
                        writer.add_page(page)
                    progress(min(95, (index + 1) * 95 // len(request.paths)))
                writer.write(temporary)
            finally:
                writer.close()
        with temporary.open("rb") as stream:
            if not _reader(stream).pages:
                raise ConversionError("병합된 PDF를 읽을 수 없습니다.")
    return destination


def transform_pdf(source: Path, destination: Path, key: str, options, progress, notice) -> None:
    with source.open("rb") as stream:
        reader = _reader(stream)
        selection = selected_pages(options.pdf_pages, len(reader.pages)) if key != "tool:pdf_compress" else list(range(len(reader.pages)))
        writer = PdfWriter()
        try:
            if key in {"tool:pdf_rotate", "tool:pdf_compress"}:
                writer.clone_document_from_reader(reader)
                selected = set(selection)
                for index, page in enumerate(checked(writer.pages)):
                    if key == "tool:pdf_rotate" and index in selected:
                        page.rotate(options.rotation)
                    elif key == "tool:pdf_compress":
                        page.compress_content_streams(level=9)
                    progress(min(95, (index + 1) * 95 // len(writer.pages)))
                if key == "tool:pdf_compress":
                    writer.compress_identical_objects()
            else:
                for index, number in enumerate(checked(selection)):
                    writer.add_page(reader.pages[number])
                    progress(min(95, (index + 1) * 95 // len(selection)))
            writer.write(destination)
        finally:
            writer.close()
    if key == "tool:pdf_compress" and destination.stat().st_size >= source.stat().st_size:
        copy_file(source, destination)
        notice("이미 최적화된 PDF여서 원본 내용을 복사했습니다.")
    with destination.open("rb") as stream:
        expected = len(reader.pages) if key in {"tool:pdf_rotate", "tool:pdf_compress"} else len(selection)
        if len(_reader(stream).pages) != expected:
            raise ConversionError("출력 PDF의 페이지 수가 올바르지 않습니다.")
    check_cancelled()


def split_pdf(source: Path, folder: Path, options, progress) -> None:
    with source.open("rb") as stream:
        reader = _reader(stream)
        pages = selected_pages(options.pdf_pages, len(reader.pages))
        for index, number in enumerate(checked(pages), start=1):
            writer = PdfWriter()
            try:
                writer.add_page(reader.pages[number])
                writer.write(folder / f"{index:04d}_page_{number + 1:04d}.pdf")
            finally:
                writer.close()
            progress(min(99, index * 99 // len(pages)))
