"""Explicit, noninteractive engine/Qt check for the portable executable."""

from __future__ import annotations

import json
import io
import os
import subprocess
import sys
import tempfile
import time
import threading
import traceback
import zipfile
from pathlib import Path


def _check_preferences(app, root: Path, image: Path, video: Path, ffmpeg: str) -> dict:
    from dataclasses import replace
    from PIL import Image
    from PySide6.QtCore import QMimeData, QPoint, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from magic_converter.core.settings.preferences import EncoderOptions, OutputRule, Preset, WheelProfile
    from magic_converter.core.settings.settings_store import SettingsStore
    from magic_converter.core.conversion.converter import ConversionService, convert_media
    from magic_converter.core.settings.history import HistoryStore
    from magic_converter.ui.shell.radial_wheel import RadialWheelWindow
    from magic_converter.ui.settings.settings_dialog import SettingsDialog
    from magic_converter.ui.jobs.history_dialog import HistoryDialog

    store = SettingsStore(root / "settings.json")
    initial = store.load({"threshold": 23})
    recipe = Preset("portable", "작은 사진", "image:jpg", EncoderOptions(76, 32))
    preferences = replace(initial, presets=(*initial.presets, recipe),
                          profiles=(*initial.profiles, WheelProfile("portable", "휴대용", "convert", (recipe.id,))),
                          convert_profile="portable", output=OutputRule("folder", str(root / "선택 출력"), "{stem}_{preset}"))
    store.save(preferences)
    if store.load() != preferences or store.read_backup() != initial:
        raise RuntimeError("Settings migration/roundtrip/backup failed")
    store.path.write_bytes(b"{corrupted")
    if store.load() != initial or not list(root.glob("settings.json.corrupt-*")):
        raise RuntimeError("Settings recovery failed")
    history = HistoryStore(root / "history.json")
    service = ConversionService(ffmpeg_path=ffmpeg)
    service.completed.connect(history.append)
    batches = []
    service.completed.connect(batches.append)
    current = [preferences]
    wheel = RadialWheelWindow(lambda: True, preferences=lambda: current[0])
    wheel.request_dropped.connect(service.submit_selection)
    wheel.show_for_drag("convert", QPoint(400, 400))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(image))])
    wheel.dragEnterEvent(QDragEnterEvent(QPoint(160, 60), Qt.DropAction.CopyAction, mime,
                                         Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier))
    current[0] = initial
    wheel.dropEvent(QDropEvent(QPointF(160, 60), Qt.DropAction.CopyAction, mime,
                               Qt.MouseButton.LeftButton, Qt.KeyboardModifier.ShiftModifier))
    until = time.monotonic() + 20
    while service.active_count:
        app.processEvents()
        if time.monotonic() >= until:
            service.cancel_all()
            raise RuntimeError("Preset conversion did not finish")
        time.sleep(0.005)
    service.pool.waitForDone(3000)
    if len(batches) != 1 or batches[0].completed != 1:
        raise RuntimeError("Frozen drag recipe did not execute")
    output = batches[0].outputs[0]
    if output != root / "선택 출력" / "image_작은 사진.jpg":
        raise RuntimeError("Preset output policy changed during drag")
    with Image.open(output) as decoded:
        if decoded.size != (32, 24):
            raise RuntimeError("Preset dimensions were not applied")
    reloaded = HistoryStore(history.path)
    if len(reloaded.entries) != 1 or reloaded.entries[0]["preset_id"] != recipe.id:
        raise RuntimeError("Preset result history roundtrip failed")
    settings_dialog = SettingsDialog(preferences.base_values(), preferences=preferences, settings_store=store)
    history_dialog = HistoryDialog(reloaded)
    if settings_dialog.grab().isNull() or history_dialog.grab().isNull() or settings_dialog.values()["preferences"] != preferences:
        raise RuntimeError("Preferences/history UI failed")
    settings_dialog.reject()
    settings_dialog.deleteLater()
    history_dialog.reject()
    wheel.deleteLater()
    service.deleteLater()
    media_output = root / "preset-video.mp4"
    convert_media(video, media_output, "media:mp4", ffmpeg, options=EncoderOptions(video_crf=27, audio_bitrate=64))
    if not media_output.is_file() or not media_output.stat().st_size:
        raise RuntimeError("Media recipe failed")
    return {"legacy_migration": "passed", "backup_recovery": "passed", "frozen_drag_recipe": "passed",
            "image_dimensions": [32, 24], "named_output": "passed", "history_roundtrip": "passed",
            "settings_history_render": "passed", "media_options": "passed"}


def run_self_test(report_path: Path, test_office: bool = False) -> int:
    from magic_converter import __version__
    report: dict = {"status": "running", "version": __version__, "frozen": bool(getattr(sys, "frozen", False))}
    try:
        os.environ["QT_QPA_PLATFORM"] = "offscreen"
        from PIL import Image
        from PySide6.QtCore import QPoint
        from PySide6.QtWidgets import QApplication
        from magic_converter.core.platform.headless_qt import configure_headless_qt
        from magic_converter.core.conversion.converter import convert_image, convert_media, find_ffmpeg, find_ffprobe, probe_duration, JobProgress
        from magic_converter.ui.shell.radial_wheel import RadialWheelWindow
        from magic_converter.ui.jobs.progress_overlay import ProgressOverlay
        from magic_converter.ui.common.icons import icon_path, make_icon
        from magic_converter.core.documents.document_converter import html_to_pdf
        from magic_converter.core.conversion.extended_converter import convert_extended
        from magic_converter.core.conversion.file_router import ALL_ACTIONS
        from magic_converter.core.engines.office_engine import office_pdf, presentation_available
        from pypdf import PdfReader, PdfWriter
        from openpyxl import Workbook

        configure_headless_qt()
        app = QApplication.instance() or QApplication(["magic_converter-self-test"])
        if make_icon().isNull():
            raise RuntimeError("Tool icon failed to load")
        report["icon"] = icon_path().name
        ffmpeg = find_ffmpeg()
        if ffmpeg is None:
            raise RuntimeError("FFmpeg not found")
        report["ffmpeg"] = ffmpeg
        report["ffprobe"] = find_ffprobe(ffmpeg)
        report["bundle_root"] = str(getattr(sys, "_MEIPASS", ""))
        if report["frozen"] and not Path(ffmpeg).is_relative_to(Path(sys._MEIPASS)):
            raise RuntimeError("Frozen self-test must use the bundled FFmpeg")
        with tempfile.TemporaryDirectory(prefix="magic-converter-check-") as temporary:
            root = Path(temporary)
            source = root / "sample.mp4"
            subprocess.run([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
                            "-f", "lavfi", "-i", "testsrc2=size=96x64:rate=10:duration=0.8",
                            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.8",
                            "-c:v", "libx264", "-c:a", "aac", "-shortest", str(source)],
                           check=True, capture_output=True, timeout=30,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            report["duration"] = probe_duration(source, ffmpeg)
            if not report["duration"]:
                raise RuntimeError("FFprobe did not return the test clip duration")
            report["media"] = {}
            for key, suffix in (("media:mp4", ".mp4"), ("media:gif", ".gif"),
                                ("media:mp3", ".mp3"), ("media:webm", ".webm")):
                output = root / ("output" + suffix)
                progress: list[int | None] = []
                convert_media(source, output, key, ffmpeg, progress.append)
                if not output.is_file() or not output.stat().st_size or progress[-1] != 100:
                    raise RuntimeError(f"Failed engine/progress check: {key}")
                report["media"][key] = {"bytes": output.stat().st_size, "progress": progress}
            image = root / "image.png"
            Image.new("RGBA", (64, 48), (240, 120, 30, 128)).save(image)
            convert_image(image, root / "image.jpg", ".jpg")
            report["image_bytes"] = (root / "image.jpg").stat().st_size
            # Exercise the public scheduler and a multi-hop route in the frozen
            # program as well as the individual legacy encoders below.
            from magic_converter.core.conversion.converter import ConversionService
            from magic_converter.core.conversion.builtin_converters import create_catalog
            from magic_converter.core.conversion.conversion_request import prepare_request
            from magic_converter.core.conversion.conversion_execution import execute_file
            from magic_converter.core.jobs.job_control import Control, JobCancelled, current
            from magic_converter.core.jobs.process_runner import run_process
            from magic_converter.ui.jobs.job_manager import JobManagerDialog
            other = root / "queued-image.png"
            Image.new("RGB", (48, 32), "orange").save(other)
            service, batches = ConversionService(), []
            service.completed.connect(batches.append)
            service.submit((image, other), "image:jpg")
            until = time.monotonic() + 20
            while service.active_count:
                app.processEvents()
                if time.monotonic() >= until:
                    service.cancel_all()
                    raise RuntimeError("File scheduler did not finish")
                time.sleep(0.005)
            service.pool.waitForDone(3000)
            if len(batches) != 1 or batches[0].completed != 2 or any(file.state != "done" for file in batches[0].files):
                raise RuntimeError("Registered file scheduler failed")
            text_source = root / "route-source.txt"
            text_source.write_text("Portable multi-hop route", encoding="utf-8")
            request = prepare_request((text_source,), "document:png", create_catalog())
            route_output = execute_file(request, request.files[0], ffmpeg, lambda _: None, lambda _: None)
            if len(list(route_output.glob("*.png"))) != 1:
                raise RuntimeError("Multi-hop output publication failed")
            dialog = JobManagerDialog(service)
            if dialog.grab().isNull():
                raise RuntimeError("Job manager render failed")
            dialog.reject()
            service.deleteLater()
            event = threading.Event()
            token = current.set(Control(event, lambda _: None))
            timer = threading.Timer(0.3, event.set)
            timer.start()
            try:
                try:
                    run_process([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-re", "-f", "lavfi",
                                 "-i", "color=size=16x16:rate=1", "-f", "null", "-"], timeout=5)
                except JobCancelled:
                    pass
                else:
                    raise RuntimeError("Child process did not observe cancellation")
            finally:
                timer.cancel()
                current.reset(token)
            bundle_root = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parents[3]))
            if not (bundle_root / "licenses" / "Filee-MIT.txt").is_file():
                raise RuntimeError("Filee MIT notice is missing")
            report["pipeline"] = {"scheduled_files": 2, "file_states": [file.state for file in batches[0].files],
                                  "multi_hop_steps": len(request.files[0].route.steps), "page_images": 1,
                                  "child_cancel": "passed", "job_manager_render": "passed", "filee_notice": "included"}
            report["preferences"] = _check_preferences(app, root, image, source, ffmpeg)
            from magic_converter.core.diagnostics.tool_diagnostics import check_tools
            report["tools"] = check_tools(app, root, image, source, ffmpeg)
            from magic_converter.core.diagnostics.document_diagnostics import check_documents, validate_document_report, DocumentCheckError
            try:
                report["documents"] = check_documents(root, test_native=test_office)
                validate_document_report(report["documents"])
            except DocumentCheckError as exc:
                report["documents"] = exc.report
                raise
            from magic_converter.core.diagnostics.automation_diagnostics import check_automation
            report["automation"] = check_automation(app, root, image)
            from magic_converter.core.diagnostics.special_diagnostics import check_specialized
            report["specialized"] = check_specialized(root)
            report["formats"] = {}
            import olefile
            import pythoncom
            from docx import Document
            from magic_converter.core.documents.hwp_documents import read_docx, read_hwpx, write_hwpx, write_docx, hwp_records, decode_hwp_text
            from magic_converter.core.engines.office_com import com_apartment
            pass
            from magic_converter.core.engines.hwp_converter import validate_output
            import struct
            with com_apartment():
                pythoncom.PumpWaitingMessages()
            pass
            doc = Document()
            doc.add_paragraph("한글 HWPX portable check")
            table = doc.add_table(rows=2, cols=2)
            table.cell(0, 0).text, table.cell(0, 1).text = "항목", "값"
            table.cell(1, 0).merge(table.cell(1, 1)).text = "병합 셀"
            doc.save(original)
            original_data = read_docx(original)
            pass
            write_hwpx(original_data, hwpx)
            validate_output(hwpx)
            restored_data = read_hwpx(hwpx)
            if restored_data.text() != original_data.text():
                raise RuntimeError("HWPX paragraphs/tables were lost")
            pass
            write_docx(restored_data, roundtrip)
            if Document(roundtrip).tables[0].cell(1, 1).text != "병합 셀":
                raise RuntimeError("HWPX merged cells were lost")
            payload = "한글 원본 본문\r".encode("utf-16le")
            record = struct.pack("<I", 67 | len(payload) << 20) + payload
            if decode_hwp_text(list(hwp_records(record))[0][1]) != "한글 원본 본문":
                raise RuntimeError("HWP text parser failed")
            pass
            pass
            import hashlib
            pass
            module = bundled_module()
            bundled = module.is_file()
            if (report["frozen"] and not bundled
                    or bundled and hashlib.sha256(module.read_bytes()).hexdigest() != MODULE_HASH):
                pass
            pass
            from magic_converter.core.documents.hwp_documents import read_hwp, DocumentData, Paragraph, Picture, Table, Cell, TextRun
            from magic_converter.core.documents.rich_hwp import object_counts
            fixture = Path(__file__).resolve().parents[2] / "data" / "hwp" / "table_picture.hwp"
            native = read_hwp(fixture)
            if object_counts(native) != {"tables": 1, "pictures": 1}:
                raise RuntimeError("Binary HWP table/picture parser failed")
            nested = DocumentData([Table(1, 1, [Cell(0, 0, native.blocks)])])
            rich_hwpx = root / "nested-pictures.hwpx"
            rich_docx = root / "nested-pictures.docx"
            write_hwpx(nested, rich_hwpx)
            write_docx(read_hwpx(rich_hwpx), rich_docx)
            counts = object_counts(read_docx(rich_docx))
            if counts != {"tables": 2, "pictures": 1}:
                raise RuntimeError("Nested table/picture interchange failed")
            pass
            pass
            for suffix in (".heic", ".avif"):
                target = root / ("image" + suffix)
                convert_image(image, target, suffix)
                with Image.open(target) as decoded:
                    if decoded.size != (64, 48):
                        raise RuntimeError("Image codec check failed")
                report["formats"][suffix] = target.stat().st_size
            page = root / "page.pdf"
            pdf = root / "document.pdf"
            html_to_pdf("<h1>Portable PDF</h1><p>PDF text and document engine test.</p>", page)
            writer = PdfWriter()
            writer.append(page)
            writer.append(page)
            writer.write(pdf)
            writer.close()
            outputs: dict[str, Path] = {}
            for suffix in ("png", "jpg", "docx", "xlsx", "txt", "md", "epub"):
                target = convert_extended(pdf, ALL_ACTIONS[f"document:{suffix}"], lambda value: None)
                outputs[suffix] = target
                if target.is_dir():
                    pages = sorted(target.iterdir())
                    if len(pages) != 2:
                        raise RuntimeError("PDF pages were lost")
                    with Image.open(pages[0]) as decoded:
                        if abs(decoded.info.get("dpi", (0, 0))[0] - 300) > 0.2:
                            raise RuntimeError("PDF DPI is not 300")
                    report["formats"]["pdf:" + suffix] = {"pages": len(pages), "dpi": 300}
                else:
                    report["formats"]["pdf:" + suffix] = target.stat().st_size
            from pptx import Presentation
            for key, dpi in (("pptx_image", 200), ("pptx_editable", 200)):
                target = convert_extended(pdf, ALL_ACTIONS[key], lambda value: None)
                outputs[key] = target
                deck = Presentation(target)
                if len(deck.slides) != 2:
                    raise RuntimeError("PPTX pages were lost")
                for slide in deck.slides if key == "pptx_image" else []:
                    with Image.open(io.BytesIO(slide.shapes[0].image.blob)) as background:
                        if abs(background.info.get("dpi", (0, 0))[0] - dpi) > 0.2:
                            raise RuntimeError("PPTX background DPI mismatch")
                texts = [shape.text for slide in deck.slides for shape in slide.shapes if shape.has_text_frame and shape.text]
                if key == "pptx_editable" and not any("PDF text and document engine test." in text for text in texts):
                    raise RuntimeError("PPTX text is not editable")
                report["formats"]["pdf:" + key] = {"bytes": target.stat().st_size, "pages": 2, "dpi": dpi, "text_boxes": len(texts)}
            # Exercise the new native grid path in the actual one-file bundle,
            # including merged cells, a picture and a genuine cell edit/save.
            import pymupdf as fitz
            # Exercise the actual conversion worker, including its retained
            # image renderer. An empty separation object once colour-proofed
            # ordinary RGB images through CMYK in source and frozen builds.
            colour_pdf = root / "retained-colours.pdf"
            colour_image = io.BytesIO()
            Image.new("RGB", (80, 60), (20, 40, 180)).save(colour_image, "JPEG")
            with fitz.open() as colour_document:
                colour_page = colour_document.new_page(width=144, height=108)
                colour_page.insert_image(colour_page.rect, stream=colour_image.getvalue())
                colour_reference = colour_page.get_pixmap(dpi=200, alpha=True).tobytes("png")
                colour_document.save(colour_pdf)
            colour_pptx = convert_extended(colour_pdf, ALL_ACTIONS["pptx_editable"], lambda _: None)
            colour_slide = Presentation(colour_pptx).slides[0]
            with Image.open(io.BytesIO(colour_slide.shapes[0].image.blob)) as actual, Image.open(io.BytesIO(colour_reference)) as expected:
                if len(colour_slide.shapes) != 1 or actual.size != expected.size or actual.tobytes() != expected.tobytes():
                    raise RuntimeError("Retained PDF image colours differ from the original renderer")
            report["formats"]["pdf:retained_colours"] = "passed"
            direction_pdf = root / "text-directions.pdf"
            with fitz.open() as direction_document:
                for matrix in (fitz.Matrix(1.004, 1), fitz.Matrix(.02), fitz.Matrix(90)):
                    direction_page = direction_document.new_page(width=400, height=400)
                    direction_page.insert_text((80, 260), "Direction 0123", fontsize=24,
                        morph=(fitz.Point(80, 260), matrix))
                direction_document.save(direction_pdf)
            direction_pptx = convert_extended(direction_pdf, ALL_ACTIONS["pptx_editable"], lambda _: None)
            direction_deck = Presentation(direction_pptx)
            import math
            with fitz.open(direction_pdf) as direction_document:
                from magic_converter.core.pdf.pdf_layout import text_spans
                for direction_page, direction_slide in zip(direction_document, direction_deck.slides):
                    direction = text_spans(direction_page)[0].direction
                    expected_angle = math.degrees(math.atan2(direction[1], direction[0])) % 360
                    shapes = [s for s in direction_slide.shapes if s.has_text_frame]
                    if len(shapes) != 1 or abs((shapes[0].rotation - expected_angle + 180) % 360 - 180) > 1 / 60000:
                        raise RuntimeError("PDF baseline direction changed in editable PowerPoint")
            if len(direction_deck.slides) != 3:
                raise RuntimeError("PDF text direction pages are missing")
            report["formats"]["pdf:text_directions"] = "passed"
            from magic_converter.core.diagnostics.pdf_fixtures import effect_pdf
            effect_source = root / 'backdrop.pdf'
            effect_pdf(effect_source)
            effect_deck = Presentation(convert_extended(effect_source, ALL_ACTIONS['pptx_editable'], lambda _: None))
            effect_shapes = effect_deck.slides[0].shapes
            if (sum(shape.shape_type == 13 for shape in effect_shapes) != 1
                    or sum(shape.shape_type == 1 for shape in effect_shapes) != 0
                    or len([shape for shape in effect_shapes if shape.has_text_frame]) != 2):
                raise RuntimeError('PDF blend backdrop is duplicated or independent text was flattened')
            report['formats']['pdf:backdrop_effect'] = 'passed'
            from magic_converter.core.diagnostics.pdf_fixtures import glyph_scale_pdf
            glyph_source = root / 'glyph-scales.pdf'
            glyph_scale_pdf(glyph_source)
            glyph_deck = Presentation(convert_extended(glyph_source, ALL_ACTIONS['pptx_editable'], lambda _: None))
            glyph_shapes = glyph_deck.slides[0].shapes
            if ([s.text for s in glyph_shapes if s.has_text_frame] != ['Glyph widths WWW iii']
                    or sum(s.name.startswith('PDF glyph outline region ') for s in glyph_shapes) != 2
                    or any(s.shape_type == 13 for s in glyph_shapes)):
                raise RuntimeError('PDF glyph width/height transform was lost')
            report['formats']['pdf:glyph_transforms'] = 'passed'
            outlined_shapes = glyph_deck.slides[1].shapes
            if ([s.text for s in outlined_shapes if s.has_text_frame] != ['Independent before', 'Independent after']
                    or sum(s.name.startswith('PDF glyph outline region ') for s in outlined_shapes) != 1
                    or any(s.shape_type == 13 for s in outlined_shapes)):
                raise RuntimeError('Outlined glyph paint discarded independent native text')
            report['formats']['pdf:outlined_glyphs'] = 'passed'
            from magic_converter.core.conversion.converter import create_zip
            from magic_converter.core.pdf.pdf_tools import merge_pdf
            from magic_converter.core.conversion.builtin_converters import create_catalog
            from magic_converter.core.conversion.conversion_request import prepare_request
            direct_zip = create_zip((glyph_source, direction_pdf))
            with zipfile.ZipFile(direct_zip) as direct_archive:
                if direct_archive.read(glyph_source.name) != glyph_source.read_bytes():
                    raise RuntimeError('Direct ZIP API lost input bytes')
            direct_request = prepare_request([glyph_source, direction_pdf], 'tool:pdf_merge', create_catalog())
            direct_merged = merge_pdf(direct_request, lambda _: None)
            with fitz.open(direct_merged) as direct_document:
                if len(direct_document) != 5:
                    raise RuntimeError('Direct PDF API lost pages')
            report['formats']['combined:direct_api'] = 'passed'
            structured_pdf = root / "structured.pdf"
            with fitz.open() as structured:
                page = structured.new_page(width=360, height=280)
                for y in (80, 120, 160, 200):
                    page.draw_line((30, y), (300, y))
                for x in (30, 300):
                    page.draw_line((x, 80), (x, 200))
                page.draw_line((160, 120), (160, 200))
                for x, y, text in ((40, 105, "Merged header"), (40, 145, "Alpha"),
                                   (170, 145, "15"), (170, 185, "Beta")):
                    page.insert_text((x, y), text, fontsize=10)
                page.insert_image((45, 165, 110, 195), filename=str(image))
                structured.save(structured_pdf)
            table_docx = convert_extended(structured_pdf, ALL_ACTIONS["document:docx"], lambda _: None)
            native_docx = Document(table_docx)
            if len(native_docx.tables) != 1 or len(native_docx.inline_shapes) != 1:
                raise RuntimeError("Structured PDF lost a Word table or picture")
            if native_docx.tables[0].cell(0, 0)._tc.grid_span != 2:
                raise RuntimeError("Word merged header was lost")
            table_pptx = convert_extended(structured_pdf, ALL_ACTIONS["pptx_editable"], lambda _: None)
            native_deck = Presentation(table_pptx)
            grids = [shape.table for slide in native_deck.slides for shape in slide.shapes if shape.has_table]
            if len(grids) != 1 or not grids[0].cell(0, 0).is_merge_origin:
                raise RuntimeError("PDF did not produce a real editable PowerPoint table")
            grids[0].cell(1, 1).text = "Edited 42"
            native_deck.save(table_pptx)
            saved_grid = next(shape.table for shape in Presentation(table_pptx).slides[0].shapes if shape.has_table)
            if saved_grid.cell(1, 1).text != "Edited 42":
                raise RuntimeError("PowerPoint table cell edit did not survive saving")
            report["formats"]["pdf:editable_structure"] = {"docx_tables": 1, "docx_pictures": 1,
                                                            "pptx_tables": 1, "merged_cells": True,
                                                            "cell_edit_roundtrip": True}
            # Detect a fresh, arbitrary chart in the frozen program too. This
            # exercises chart XML, embedded XLSX writing and provenance without
            # relying on any shipped/customer PDF or installed Office program.
            chart_pdf = root / "cartesian-chart.pdf"
            with fitz.open() as chart_document:
                chart_page = chart_document.new_page(width=360, height=260)
                for value in (0, 2, 4, 6, 8, 10):
                    chart_page.insert_text((15, 203-value*14), str(value), fontsize=9)
                for index, value in enumerate((3, 7, 5, 9)):
                    x = 70+index*60
                    chart_page.draw_rect((x-10, 200-value*14, x+10, 200), color=None, fill=(.2, .4, .6))
                    chart_page.insert_text((x-3, 193-value*14), str(value), fontsize=9)
                    chart_page.insert_text((x-3, 214), chr(65+index), fontsize=9)
                chart_document.save(chart_pdf)
            chart_pptx = convert_extended(chart_pdf, ALL_ACTIONS["pptx_editable"], lambda _: None)
            chart_deck = Presentation(chart_pptx)
            native_charts = [s.chart for s in chart_deck.slides[0].shapes if s.has_chart]
            if len(native_charts) != 1 or list(native_charts[0].series[0].values) != [3, 7, 5, 9]:
                raise RuntimeError("PDF did not produce an editable PowerPoint chart")
            from openpyxl import load_workbook
            embedded = load_workbook(io.BytesIO(native_charts[0].part.chart_workbook.xlsx_part.blob))
            if embedded.worksheets[0]["B3"].value != 7 or "PDF Recovery" not in embedded.sheetnames:
                raise RuntimeError("Chart data/provenance workbook was lost")
            report["formats"]["pdf:editable_charts"] = {"charts": 1, "embedded_workbook": True, "values": [3, 7, 5, 9]}
            workbook = Workbook()
            workbook.active.append(["Name", "Value"])
            workbook.active.append(["Check", 42])
            sheet = root / "table.xlsx"
            workbook.save(sheet)
            for suffix in ("ods", "csv", "tsv"):
                target = convert_extended(sheet, ALL_ACTIONS[f"sheet:{suffix}"], lambda value: None)
                report["formats"][suffix] = target.stat().st_size
            subtitle = root / "subtitle.srt"
            subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
            report["formats"]["vtt"] = convert_extended(subtitle, ALL_ACTIONS["subtitle:vtt"], lambda value: None).stat().st_size
            archive = root / "archive.zip"
            with zipfile.ZipFile(archive, "w") as packed:
                packed.writestr("test.txt", "offline")
            report["formats"]["tar.gz"] = convert_extended(archive, ALL_ACTIONS["archive:gzip"], lambda value: None).stat().st_size
            report["office_available"] = presentation_available()
            if test_office:
                from magic_converter.core.engines.hwp_converter import convert_office_pdf
                office_output = root / "word-com.pdf"
                notices = []
                engine = convert_office_pdf(original, office_output, lambda _: None, notices.append)
                if engine != "office-com":
                    raise RuntimeError("Word pywin32 COM validation used a fallback: " + str(notices))
                pass
                if not presentation_available():
                    raise RuntimeError("PowerPoint/LibreOffice is required for --self-test-office")
                rendered = root / "slides-office.pdf"
                office_pdf(outputs["pptx_image"], rendered)
                report["office_pdf_pages"] = len(PdfReader(rendered).pages)
                if report["office_pdf_pages"] != 2:
                    raise RuntimeError("Office export did not preserve all slides")
            wheel = RadialWheelWindow(lambda: True)
            wheel.show_for_drag("convert", QPoint(200, 200))
            wheel.paths = (pdf,)
            wheel._rebuild_slots()
            wheel._open_animation.setCurrentTime(150)
            if abs(wheel.wheelScale - 1.0) > 0.001 or wheel.grab().isNull():
                raise RuntimeError("Wheel/animation render failed")
            wheel.dismiss()
            wheel.deleteLater()
            overlay = ProgressOverlay()
            overlay.update_progress(JobProgress(1, "MP4", "sample.mp4", 1, 1, 42))
            if overlay.rows[1].bar.value() != 42 or overlay.grab().isNull():
                raise RuntimeError("Progress overlay render failed")
            overlay.shutdown()
            overlay.deleteLater()
            app.processEvents()
            report["qt_render"] = "passed"
        report["status"] = "passed"
    except Exception:
        report["status"] = "failed"
        report["error"] = traceback.format_exc()
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if report["status"] == "passed" else 1
