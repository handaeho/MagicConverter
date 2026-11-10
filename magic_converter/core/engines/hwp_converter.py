
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import tempfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.documents.hwp_documents import (Cell, DocumentData, Paragraph, Picture, Table, TextRun, document_html,
                               read_docx, read_hwp, read_hwpx, write_docx, write_hwpx)
from magic_converter.core.jobs.job_control import check_cancelled, report_stage, JobCancelled, JobTimedOut
from magic_converter.core.engines.office_process import run_helper

LOGGER = logging.getLogger(__name__)
OFFICE_COM_LOCK = threading.Lock()


def validate_output(destination: Path) -> None:
    suffix = destination.suffix.lower()
    if not destination.is_file() or not destination.stat().st_size:
        raise ConversionError("변환 엔진이 출력 파일을 생성하지 않았습니다.")
    if suffix in {".hwpx", ".docx"}:
        with zipfile.ZipFile(destination) as archive:
            required = {"Contents/section0.xml", "Contents/header.xml", "Contents/content.hpf", "META-INF/container.xml", "mimetype"} if suffix == ".hwpx" else {"word/document.xml", "[Content_Types].xml"}
            if not required.issubset(archive.namelist()) or archive.testzip() is not None:
                raise ConversionError(f"올바른 {suffix.upper()} 문서가 아닙니다.")
            if suffix == ".hwpx" and archive.read("mimetype").strip() != b"application/hwp+zip":
                raise ConversionError("HWPX ZIP의 mimetype이 올바르지 않습니다.")
    elif suffix == ".hwp":
        import olefile
        if not olefile.isOleFile(destination):
            pass
        with olefile.OleFileIO(destination) as archive:
            if not archive.exists("FileHeader") or not archive.openstream("FileHeader").read(32).startswith(b"HWP Document File"):
                pass
    elif suffix == ".pdf":
        with destination.open("rb") as document:
            if document.read(5) != b"%PDF-":
                raise ConversionError("유효한 PDF 출력이 아닙니다.")


def run_com(source: Path, destination: Path, operation: str, *, validate: bool = True) -> None:
    """A modal COM dialog cannot block a pool thread indefinitely or the UI."""
    pass
    from magic_converter.core.jobs.office_broker import native_proxy
    proxy = native_proxy.get()
    if proxy is not None:
        proxy(source, destination, operation)
        validate_output(destination)
        return
    # Office automation servers can reuse process-global state even across
    # independent instances. Queue these jobs while other encoders keep going.
    report_stage("Office 작업 대기 중")
    while not OFFICE_COM_LOCK.acquire(timeout=0.2):
        check_cancelled()
    try:
        check_cancelled()
        pass
        _run_com(source, destination, operation)
        if validate:
            validate_output(destination)
    finally:
        OFFICE_COM_LOCK.release()


def _run_com(source: Path, destination: Path, operation: str) -> None:
    with tempfile.TemporaryDirectory(prefix="magic-office-job-") as directory:
        root = Path(directory)
        request, report = root / "request.json", root / "report.json"
        request.write_text(json.dumps({"source": str(source.resolve()), "destination": str(destination.resolve()),
                                       "operation": operation}, ensure_ascii=False), encoding="utf-8")
        command = ([sys.executable, "--office-com"] if getattr(sys, "frozen", False)
                   else [sys.executable, "-m", "magic_converter.core.engines.office_com"])
        try:
            helper_options = {'timeout': 180} if operation == 'excel_print' else {}
            if operation in {'office_pdf', 'excel_print'} and source.suffix.lower() == '.xlsx':
                from magic_converter.core.jobs.job_control import current
                control = current.get()
                if control is not None and control.deadline is not None:
                    check_cancelled()
                    remaining = max(.001, control.deadline - time.monotonic())
                    helper_options['timeout'] = min(180, remaining) if operation == 'excel_print' else remaining
            code, errors = run_helper([*command, str(request), str(report)], report.with_suffix(".state.json"),**helper_options)
            outcome = json.loads(report.read_text(encoding="utf-8")) if report.is_file() else {}
            if code or not outcome.get("ok"):
                raise ConversionError(outcome.get("error") or errors[-1200:]
                                      or "Office COM 변환 프로세스가 종료되었습니다.")
        except JobTimedOut:
            raise
        except TimeoutError as exc:
            raise ConversionError(str(exc)) from exc
        finally:
            cleanup_owned_processes(report.with_suffix(".processes.json"))


def office_content(source: Path) -> DocumentData:
    if source.suffix.lower() == ".docx":
        return read_docx(source)
    if source.suffix.lower() == ".xlsx":
        from openpyxl import load_workbook
        from openpyxl.cell.cell import MergedCell
        result = DocumentData()
        workbook = load_workbook(source, read_only=False, data_only=False, keep_links=False)
        try:
            for sheet in workbook:
                check_cancelled()
                result.blocks.append(Paragraph([TextRun(sheet.title, 14, True)]))
                if sheet.max_row * sheet.max_column > 100000:
                    raise ConversionError("문서 모델 변환은 시트당 100,000셀까지 지원합니다.")
                spans = {(m.min_row, m.min_col): (m.max_row - m.min_row + 1, m.max_col - m.min_col + 1)
                         for m in sheet.merged_cells.ranges}
                cells = []
                for row in sheet.iter_rows():
                    check_cancelled()
                    for cell in row:
                        if isinstance(cell, MergedCell):
                            continue
                        rowspan, colspan = spans.get((cell.row, cell.column), (1, 1))
                        color = cell.fill.fgColor
                        fill = color.rgb[-6:] if cell.fill.patternType == "solid" and color.type == "rgb" else None
                        text = TextRun("" if cell.value is None else str(cell.value), size=cell.font.sz or 10,
                                       bold=bool(cell.font.bold), italic=bool(cell.font.italic), font=cell.font.name or "맑은 고딕")
                        cells.append(Cell(cell.row - 1, cell.column - 1, [Paragraph([text])], rowspan, colspan, fill))
                result.blocks.append(Table(sheet.max_row, sheet.max_column, cells))
                for picture in sheet._images:
                    result.blocks.append(Paragraph([Picture(picture._data(), picture.width * .75, picture.height * .75)]))
                if sheet._charts:
                    result.warnings.append("Excel 차트는 문서 모델 출력에 포함되지 않습니다.")
        finally:
            workbook.close()
        result.warnings.append("통합 문서의 셀 값·수식 문자열·그림을 문서로 옮겼습니다. 셀 서식과 인쇄 배치는 달라질 수 있습니다.")
        return result
    from pptx import Presentation
    result = DocumentData()
    def shapes(collection):
        for shape in collection:
            if hasattr(shape, "shapes"):
                yield from shapes(shape.shapes)
            else:
                yield shape
    for index, slide in enumerate(Presentation(source).slides):
        result.blocks.append(Paragraph([TextRun(f"슬라이드 {index + 1}", 14, True)]))
        for shape in shapes(slide.shapes):
            if shape.shape_type == 13:
                result.blocks.append(Paragraph([Picture(shape.image.blob, shape.width / 12700, shape.height / 12700,
                                                        shape.image.filename)]))
            if shape.has_text_frame:
                for p in shape.text_frame.paragraphs:
                    result.blocks.append(Paragraph([TextRun(p.text)]))
            if shape.has_table:
                table = shape.table
                result.blocks.append(Table(len(table.rows), len(table.columns),
                    [Cell(r, c, [Paragraph([TextRun(cell.text)])], cell.span_height, cell.span_width)
                     for r, row in enumerate(table.rows) for c, cell in enumerate(row.cells) if not cell.is_spanned]))
    result.warnings.append("슬라이드의 본문·표·그림을 문서 순서로 옮겼습니다. 차트·도형·애니메이션과 원래 배치는 보존하지 않습니다.")
    return result


def read_content(source: Path) -> DocumentData:
    from magic_converter.core.engines.office_formats import VARIANTS, canonical, normalize
    if source.suffix.lower() in VARIANTS:
        with tempfile.TemporaryDirectory(prefix="magic-office-model-") as directory:
            intermediate = Path(directory) / ("model" + canonical(source.suffix.lower()))
            normalize(source, intermediate)
            return office_content(intermediate)
    if source.suffix.lower() == ".hwpx":
        return read_hwpx(source)
    if source.suffix.lower() == ".hwp":
        from magic_converter.core.engines import rhwp_engine
        if rhwp_engine.locate():
            with tempfile.TemporaryDirectory(prefix="magic-hwp-model-") as directory:
                intermediate = Path(directory) / "model.hwpx"
                try:
                    engine_warnings = []
                    rhwp_engine.convert(source, intermediate, warning=engine_warnings.append)
                    data = read_hwpx(intermediate)
                    data.warnings.extend(engine_warnings)
                    from magic_converter.core.documents.document_audit import hwp_object_counts, inventory
                    expected, actual = hwp_object_counts(source), inventory(data)
                    for key, label in (("tables", "표"), ("pictures", "그림"), ("equations", "수식")):
                        if expected[key] != len(actual[key]):
                            data.warnings.append(f"HWP 가져오기 손실 검사: 원본 {label} {expected[key]}개 / 문서 모델 {len(actual[key])}개.")
                    return data
                except (JobCancelled, JobTimedOut):
                    raise
                except Exception as exc:
                    LOGGER.warning("rhwp model import failed: %s", exc)
        data = read_hwp(source)
        data.warnings.append("기본 HWP 레코드 리더를 사용했습니다. 지원하지 않는 수식·도형은 누락될 수 있습니다.")
        return data
    return office_content(source)


def audit_output(source: Path, destination: Path, warning, data=None) -> None:
    from magic_converter.core.documents.document_audit import compare, inventory
    try:
        before = data or read_content(source)
        if destination.suffix.lower() == ".pdf":
            import fitz
            with fitz.open(destination) as document:
                if not len(document) or any(page.rect.is_empty for page in document):
                    raise ConversionError("PDF 페이지가 비어 있습니다.")
            for message in before.warnings:
                warning(message)
            counts = inventory(before)
            warning(f"PDF 구조 검사: 페이지 유효. 원본 표 {len(counts['tables'])}·그림 {len(counts['pictures'])}·수식 {len(counts['equations'])}개는 시각 확인이 필요합니다.")
        else:
            for message in compare(before, read_content(destination)):
                warning(message)
    except (JobCancelled, JobTimedOut):
        raise
    except Exception as exc:
        warning(f"손실 검사를 완료하지 못했습니다: {str(exc)[:180]}")


def convert_hwp(source: Path, destination: Path, progress: Callable[[int | None], None],
                   warning: Callable[[str], None] = lambda _: None, *, prefer_native: bool = True) -> None:
    ext, target = source.suffix.lower(), destination.suffix.lower()
    data = None
    if ext in {".hwp", ".hwpx"} and target == ".txt":
        data = read_content(source)
        for message in data.warnings:
            warning(message)
        destination.write_text(data.text(), encoding="utf-8-sig")
        progress(100)
        return
    enabled = prefer_native and automation_enabled()
    pass
    if available:
        progress(None)
        try:
            from magic_converter.core.documents.document_audit import inventory
            if ext == ".docx":
                data = office_content(source)
            if ext in {".pptx", ".xlsx"} or data and inventory(data)["equations"]:
                with tempfile.TemporaryDirectory(prefix="magic-office-text-") as directory:
                    intermediate = Path(directory) / "document.hwpx"
                    data = data or office_content(source)
                    write_hwpx(data, intermediate)
                    pass
                pass
                for message in data.warnings:
                    warning(message)
            else:
                pass
            messages = []
            audit_output(source, destination, messages.append, data)
            for message in messages:
                warning(message)
            if any(message.startswith("손실 검사:") for message in messages):
                pass
            progress(100)
            return
        except (JobCancelled, JobTimedOut):
            raise
        except Exception as exc:
            pass
            destination.unlink(missing_ok=True)
            pass
    else:
        pass
    progress(20)
    from magic_converter.core.engines import rhwp_engine
    if rhwp_engine.locate() and (target == ".hwp" or ext in {".hwp", ".hwpx"} and target in {".hwpx", ".pdf"}):
        try:
            with tempfile.TemporaryDirectory(prefix="magic-rhwp-") as directory:
                intermediate = source
                if ext not in {".hwp", ".hwpx"}:
                    data = data or read_content(source)
                    intermediate = Path(directory) / "model.hwpx"
                    write_hwpx(data, intermediate)
                # Complex-script PDFs use Word/Qt, which perform text shaping.
                if target == ".pdf":
                    data = data or read_content(source)
                    if rhwp_engine.shaping_required(data.text()):
                        raise ConversionError("복합 문자 조판을 위해 대체 PDF 엔진을 사용합니다.")
                rhwp_engine.convert(intermediate, destination, warning=warning)
            pass
            audit_output(source, destination, warning, data)
            progress(100)
            return
        except (JobCancelled, JobTimedOut):
            raise
        except Exception as exc:
            destination.unlink(missing_ok=True)
            if target == ".hwp":
                raise ConversionError(f"HWP 저장 실패: {exc}") from exc
            warning(f"rhwp 대체 경로 실패: {str(exc)[:180]}")
    elif target == ".hwp":
        pass
    report_stage("문서 변환 중")
    data = data or read_content(source)
    if target == ".docx":
        write_docx(data, destination)
    elif target == ".hwpx":
        write_hwpx(data, destination)
    elif target == ".pdf":
        # Word lays out nested tables and embedded pictures more reliably than
        # Qt's limited HTML table renderer. Keep an entirely local Qt fallback.
        if com_available("Word.Application"):
            try:
                pass
            except (JobCancelled, JobTimedOut):
                raise
            except Exception as exc:
                LOGGER.warning("Word layout export failed: %s", exc)
                from magic_converter.core.documents.document_converter import html_to_pdf
                html_to_pdf(document_html(data), destination)
                warning("Word 출력에 실패하여 기본 PDF 엔진을 사용했습니다.")
        else:
            from magic_converter.core.documents.document_converter import html_to_pdf
            html_to_pdf(document_html(data), destination)
    else:
        raise ConversionError(f"지원하지 않는 한글 변환입니다: {ext} → {target}")
    validate_output(destination)
    audit_output(source, destination, warning, data)
    warning("문서 모델로 변환했습니다. 글꼴·떠 있는 개체 위치와 페이지 나눔은 원본과 다를 수 있습니다.")
    progress(100)


def convert_office_pdf(source: Path, destination: Path, progress: Callable[[int | None], None],
                       warning: Callable[[str], None] = lambda _: None) -> str:
    from magic_converter.core.documents.large_spreadsheet import large_workbook, stream_workbook_pdf
    if large_workbook(source):
        progress(None)
        summary=stream_workbook_pdf(source,destination)
        warning(f'대형 XLSX 전체 셀 내용 출력: {summary["sheets"]}개 시트, {summary["rows"]}행, '
                f'{summary["cells"]}개 셀을 {summary["pages"]}쪽으로 보존했습니다. '
                '원래 인쇄 배치·서식·도형은 보존하지 않으며 빈 행은 생략하고 수식은 계산하지 않은 텍스트로 출력합니다.')
        validate_output(destination)
        progress(100)
        return 'streamed-xlsx'
    progid = {".docx": "Word.Application", ".pptx": "PowerPoint.Application", ".xlsx": "Excel.Application"}[source.suffix.lower()]
    if com_available(progid):
        try:
            progress(None)
            run_com(source, destination, "office_pdf")
            audit_output(source, destination, warning)
            progress(100)
            return "office-com"
        except (JobCancelled, JobTimedOut):
            raise
        except Exception as exc:
            LOGGER.warning("Office PDF automation failed: %s", exc)
            destination.unlink(missing_ok=True)
            warning("Office PDF 출력에 실패하여 사용 가능한 대체 엔진으로 변환했습니다.")
    from magic_converter.core.engines.office_engine import find_libreoffice, office_pdf
    engine = "qt"
    if find_libreoffice():
        office_pdf(source, destination)
        engine = "libreoffice"
    else:
        data = office_content(source)
        from magic_converter.core.engines import rhwp_engine
        if rhwp_engine.locate() and not rhwp_engine.shaping_required(data.text()):
            try:
                with tempfile.TemporaryDirectory(prefix="magic-office-rhwp-") as directory:
                    intermediate = Path(directory) / "model.hwpx"
                    write_hwpx(data, intermediate)
                    rhwp_engine.convert(intermediate, destination, warning=warning)
                engine = "rhwp"
                warning("Office PDF 대체: rhwp 문서 모델로 출력했습니다. 원래 배치는 달라질 수 있습니다.")
            except (JobCancelled, JobTimedOut):
                raise
            except Exception as exc:
                destination.unlink(missing_ok=True)
                warning(f"rhwp PDF 출력 실패: {str(exc)[:180]}")
        if engine == "qt":
            from magic_converter.core.documents.document_converter import html_to_pdf
            html_to_pdf(document_html(data), destination)
            warning("기본 PDF 엔진으로 본문·표·그림을 출력했습니다. 수식은 스크립트 텍스트이며 원래 배치는 달라질 수 있습니다.")
    validate_output(destination)
    audit_output(source, destination, warning)
    progress(100)
    return engine
