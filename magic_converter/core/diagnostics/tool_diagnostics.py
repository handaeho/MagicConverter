"""Portable smoke checks for every tool family, using only owned temporary data."""

import json
from pathlib import Path
import zipfile


def check_tools(app, root: Path, image: Path, video: Path, ffmpeg: str) -> dict:
    import pymupdf as fitz
    from PIL import Image
    from pypdf import PdfReader
    from magic_converter.core.conversion.builtin_converters import create_catalog
    from magic_converter.core.conversion.conversion_request import prepare_request
    from magic_converter.core.conversion.converter import ConversionWorker
    from magic_converter.core.settings.preferences import Preset, ToolOptions, OutputRule, SCHEMA_VERSION
    from magic_converter.core.media.media_tools import probe_media
    from magic_converter.core.jobs.process_runner import run_process
    from magic_converter.core.media.subtitle_tools import read_subtitle
    from magic_converter.core.actions.tool_definitions import TOOLS
    from magic_converter.ui.settings.tool_options import ToolOptionsDialog
    from magic_converter.ui.shell.radial_wheel import RadialWheelWindow
    from PySide6.QtCore import QPoint
    directory = root / "tools"
    directory.mkdir()
    catalog = create_catalog()
    checks = []

    def run(paths, key, options=ToolOptions()):
        if isinstance(paths, Path):
            paths = (paths,)
        preset = Preset("portable-tool", "검사 도구", key, tools=options)
        request = prepare_request(paths, key, catalog, preset=preset,
                                  output=OutputRule("folder", str(directory / "outputs")))
        worker = ConversionWorker(1, request.paths, request.action, ffmpeg, request=request)
        results = []
        worker.signals.finished.connect(results.append)
        worker.run()
        if len(results) != 1 or results[0].errors or results[0].completed != len(paths):
            raise RuntimeError(f"Tool failed: {key}: {results[0].errors if results else 'no result'}")
        checks.append(key)
        return results[0].outputs[0]

    pdf = directory / "pages.pdf"
    doc = fitz.open()
    for index in range(2):
        page = doc.new_page(width=200, height=300)
        page.insert_text((20, 30), f"TOOL PAGE {index + 1}")
    doc.save(pdf)
    doc.close()
    merged = run((pdf, image), "tool:pdf_merge")
    if len(PdfReader(merged).pages) != 3:
        raise RuntimeError("PDF merge page count mismatch")
    selected = run(pdf, "tool:pdf_select", ToolOptions(pdf_pages="2,1"))
    if "PAGE 2" not in PdfReader(selected).pages[0].extract_text():
        raise RuntimeError("PDF selected page order mismatch")
    split = run(pdf, "tool:pdf_split")
    if len(list(split.glob("*.pdf"))) != 2:
        raise RuntimeError("PDF split count mismatch")
    rotated = run(pdf, "tool:pdf_rotate", ToolOptions(pdf_pages="2"))
    if [page.rotation for page in PdfReader(rotated).pages] != [0, 90]:
        raise RuntimeError("PDF rotation mismatch")
    compressed = run(pdf, "tool:pdf_compress")
    if compressed.stat().st_size > pdf.stat().st_size:
        raise RuntimeError("PDF compression increased size")
    with fitz.open(pdf) as before, fitz.open(compressed) as after:
        if before[0].get_pixmap().samples != after[0].get_pixmap().samples:
            raise RuntimeError("Lossless PDF compression changed pixels")

    transformed = run(image, "tool:image_rotate")
    with Image.open(transformed) as actual:
        if actual.size != (48, 64):
            raise RuntimeError("Image rotation dimensions mismatch")
    for key in ("tool:image_flip", "tool:image_grayscale"):
        output = run(image, key)
        with Image.open(output) as actual:
            actual.load()
    cropped = run(image, "tool:image_crop", ToolOptions(crop_x=5, crop_y=5, crop_width=20, crop_height=10))
    with Image.open(cropped) as actual:
        if actual.size != (20, 10):
            raise RuntimeError("Image crop mismatch")
    animated = directory / "animation.gif"
    frames = [Image.new("RGB", (12, 8), color) for color in ("red", "green", "blue")]
    frames[0].save(animated, save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=1)
    for frame in frames:
        frame.close()
    extracted = run(animated, "tool:image_frames")
    manifest = json.loads((extracted / "frames.json").read_text(encoding="utf-8"))
    if [frame["duration_ms"] for frame in manifest["frames"]] != [100, 200, 300]:
        raise RuntimeError("Image frame timing mismatch")

    trimmed = run(video, "tool:media_trim", ToolOptions(start_ms=200, duration_ms=300))
    duration = float(probe_media(trimmed, ffmpeg)["format"]["duration"])
    if not 0.2 <= duration <= 0.45:
        raise RuntimeError("Media trim duration mismatch")
    muted = run(video, "tool:media_mute")
    if {stream["codec_type"] for stream in probe_media(muted, ffmpeg)["streams"]} != {"video"}:
        raise RuntimeError("Muted media still contains audio")
    playback = run_process([ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(trimmed), "-f", "null", "-"], timeout=20)
    if playback.returncode:
        raise RuntimeError("Trimmed media failed full decoding")
    extracted = run(video, "tool:media_frames", ToolOptions(frame_interval=2))
    video_manifest = json.loads((extracted / "frames.json").read_text(encoding="utf-8"))
    if len(video_manifest["frames"]) != 4:
        raise RuntimeError("Video frame count mismatch")

    subtitle = directory / "captions.vtt"
    subtitle.write_text("WEBVTT\n\nintro\n00:01.000 --> 00:02.000 align:start\n한글 자막\nsecond line\n", encoding="utf-8")
    shifted = run(subtitle, "tool:subtitle_shift", ToolOptions(subtitle_offset_ms=500))
    cues, _ = read_subtitle(shifted)
    if cues[0].start_ms != 1500 or cues[0].settings != "align:start":
        raise RuntimeError("Subtitle timing/settings mismatch")
    archive = directory / "files.zip"
    with zipfile.ZipFile(archive, "w") as packed:
        packed.writestr("folder/한글.bin", bytes(range(256)))
        packed.writestr("empty/", b"")
    extracted = run(archive, "tool:archive_extract")
    if (extracted / "folder" / "한글.bin").read_bytes() != bytes(range(256)) or not (extracted / "empty").is_dir():
        raise RuntimeError("Extracted archive contents mismatch")
    options = ToolOptionsDialog("tool:pdf_select", ToolOptions(pdf_pages="2,1"))
    if options.grab().isNull() or options.value().pdf_pages != "2,1":
        raise RuntimeError("Tool options UI failed")
    options.deleteLater()
    wheel = RadialWheelWindow(lambda: True)
    wheel.show_for_drag("action", QPoint(400, 400))
    wheel.paths = (pdf,)
    wheel._rebuild_slots()
    if "tool:pdf_merge" not in {slot.action.key for slot in wheel.slots} or wheel.grab().isNull():
        raise RuntimeError("Contextual tools wheel failed")
    wheel.dismiss()
    wheel.deleteLater()
    if len(checks) != len(TOOLS):
        raise RuntimeError("A registered tool was not exercised")
    return {"actions": len(checks), "pdf_objects_pages_render": "passed", "image_pixels_frames_timing": "passed",
            "media_decode_streams_frames": "passed", "subtitle_timing": "passed", "archive_contents": "passed",
            "tool_options_wheel_render": "passed", "settings_schema": SCHEMA_VERSION}
