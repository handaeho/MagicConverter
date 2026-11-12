"""Real PDF objects, pixels, decoded media, subtitle times and archive contents."""

from dataclasses import replace
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import tarfile
import tempfile
import sys
import threading
import time
import unittest
from unittest.mock import patch
import wave
import zipfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image, ImageSequence
from pypdf import PdfReader, PdfWriter
import pymupdf as fitz
from PySide6.QtWidgets import QApplication, QDialog

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import prepare_request, RequestError
from magic_converter.core.conversion.converter import ConversionWorker, ConversionService, find_ffmpeg, find_ffprobe, ConversionError
from magic_converter.core.conversion.file_router import ALL_ACTIONS, slots_for
from magic_converter.core.jobs.job_control import Control, JobCancelled, current, check_cancelled
from magic_converter.core.settings.preferences import Preset, ToolOptions, SettingsError, default_preferences, SCHEMA_VERSION
from magic_converter.core.jobs.process_runner import run_process
from magic_converter.core.settings.settings_store import SettingsStore
from magic_converter.core.settings.json_store import write_json, read_json
from magic_converter.core.media.subtitle_tools import read_subtitle
from magic_converter.core.actions.tool_definitions import TOOLS
from magic_converter.core.media.media_tools import probe_media

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def pump_until(predicate, timeout=10):
    end = time.monotonic() + timeout
    while not predicate():
        APP.processEvents()
        if time.monotonic() >= end:
            raise AssertionError("Timed out")
        time.sleep(0.005)
    APP.processEvents()


class ToolTestCase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.catalog = create_catalog(ffmpeg_available=bool(find_ffmpeg()), presentation_available=False)

    def tearDown(self):
        APP.processEvents()
        self.temporary.cleanup()

    def run_tool(self, paths, key, tools=ToolOptions(), *, fail=False):
        if isinstance(paths, Path):
            paths = (paths,)
        preset = Preset("test", "테스트 도구", key, tools=tools)
        request = prepare_request(paths, key, self.catalog, preset=preset)
        worker = ConversionWorker(1, request.paths, request.action, find_ffmpeg(), request=request)
        results = []
        worker.signals.finished.connect(results.append)
        if fail:
            with self.assertLogs("magic_converter.core.conversion.converter", level="ERROR"):
                worker.run()
        else:
            worker.run()
        result = results[0]
        if not fail:
            self.assertFalse(result.errors)
        return result

    def pdf(self, name="pages.pdf", count=3):
        path = self.root / name
        doc = fitz.open()
        for index in range(count):
            page = doc.new_page(width=200 + index * 10, height=300)
            page.insert_text((20, 40), f"PAGE {index + 1}")
            page.add_text_annot((40, 60), f"NOTE {index + 1}")
        doc.save(path, deflate=False)
        doc.close()
        return path

class ToolTests(ToolTestCase):
    def test_original_copy_preserves_file_bytes_and_compound_suffix(self):
        source = self.root / "attachment.tar.gz"
        original = b"fixture byte sequence"
        source.write_bytes(original)
        first = self.run_tool(source, "tool:copy_original").outputs[0]
        second = self.run_tool(source, "tool:copy_original").outputs[0]
        self.assertEqual(first.name, "attachment_copy.tar.gz")
        self.assertNotEqual(first, second)
        self.assertEqual(source.read_bytes(), original)
        self.assertEqual(first.read_bytes(), original)
        self.assertEqual(second.read_bytes(), original)
        empty = self.root / "empty.txt"
        empty.touch()
        self.assertEqual(self.run_tool(empty, "tool:copy_original").outputs[0].read_bytes(), b"")

    @unittest.skipUnless(find_ffmpeg() and find_ffprobe(find_ffmpeg()), "FFmpeg and FFprobe required")
    def test_mkv_track_copy_keeps_packets_languages_chapter_and_attachment(self):
        ffmpeg = find_ffmpeg()
        ffprobe = find_ffprobe(ffmpeg)
        source = self.root / "tracks.mkv"
        subtitle = self.root / "caption.srt"
        subtitle.write_text("1\n00:00:00,100 --> 00:00:00,300\nSubtitle\n", encoding="utf-8")
        chapter = self.root / "chapters.txt"
        chapter.write_text(";FFMETADATA1\ntitle=Track test\n[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=400\ntitle=Intro\n",
                           encoding="utf-8")
        attachment = self.root / "notes.txt"
        attachment.write_bytes(b"original attachment")
        created = run_process([
            ffmpeg, "-v", "error", "-y", "-f", "lavfi", "-i", "color=s=48x32:d=0.5",
            "-f", "lavfi", "-i", "sine=f=400:d=0.5", "-f", "lavfi", "-i", "sine=f=800:d=0.5",
            "-i", str(subtitle), "-i", str(chapter),
            "-map", "0:v", "-map", "1:a", "-map", "2:a", "-map", "3:s",
            "-map_chapters", "4", "-map_metadata", "4", "-c:v", "libx264", "-c:a", "aac", "-c:s", "srt",
            "-metadata:s:a:0", "language=kor", "-metadata:s:a:1", "language=eng",
            "-attach", str(attachment), "-metadata:s:t:0", "mimetype=text/plain", str(source),
        ], timeout=30)
        self.assertEqual(created.returncode, 0, created.stderr)
        original = source.read_bytes()
        copied = self.run_tool(source, "tool:media_remux_mkv").outputs[0]

        def packets(path):
            probed = run_process([ffprobe, "-v", "error", "-show_packets", "-show_data_hash", "sha256",
                                  "-of", "json", str(path)], timeout=30)
            self.assertEqual(probed.returncode, 0, probed.stderr)
            return [(p["stream_index"], p.get("pts_time"), p.get("dts_time"), p["data_hash"])
                    for p in json.loads(probed.stdout)["packets"]]

        self.assertEqual(packets(copied), packets(source))
        info = probe_media(copied, ffmpeg, chapters=True)
        self.assertEqual([item["codec_type"] for item in info["streams"]],
                         ["video", "audio", "audio", "subtitle", "attachment"])
        self.assertEqual([item["tags"]["language"] for item in info["streams"][1:3]], ["kor", "eng"])
        self.assertEqual(info["chapters"][0]["tags"]["title"], "Intro")
        self.assertEqual(info["format"]["tags"]["title"], "Track test")
        extracted = self.root / "extracted.txt"
        extracted_result = run_process([ffmpeg, "-v", "error", "-dump_attachment:t:0", str(extracted),
                                        "-i", str(copied), "-t", "0", "-f", "null", "-"], timeout=30)
        self.assertEqual(extracted_result.returncode, 0, extracted_result.stderr)
        self.assertEqual(extracted.read_bytes(), attachment.read_bytes())
        self.assertEqual(source.read_bytes(), original)
        reencoded = self.run_tool(source, "media:mkv")
        self.assertTrue(any("추가 트랙" in warning for warning in reencoded.warnings), reencoded.warnings)

    def test_tools_are_direct_named_operations_and_default_control_wheel_keeps_shift_layout(self):
        for tool in TOOLS:
            source = next(iter(sorted(tool.sources)))
            route = self.catalog.assess("source." + source, tool.key).route
            self.assertIsNotNone(route, tool.key)
            self.assertEqual(route.steps[0].action_key, tool.key)
            self.assertEqual(route.steps[0].intent, tool.key)
            self.assertTrue(route.steps[0].direct_only)
        self.assertEqual(len(slots_for(("file.pdf",), "convert")), 10)
        self.assertIn("tool:pdf_merge", {slot.action.key for slot in slots_for(("file.pdf", "image.png"), "action")})
        self.assertNotIn("tool:pdf_merge", {slot.action.key for slot in slots_for(("file.pdf", "audio.mp3"), "action")})
        self.assertNotIn("tool:archive_extract", {slot.action.key for slot in slots_for(("file.rar",), "action")})
        with self.assertRaises(RequestError):
            prepare_request(("video.mp4",), "tool:media_frames", create_catalog(ffmpeg_available=False))

    def test_pdf_merge_combines_pdf_and_every_image_frame_in_input_order(self):
        first = self.pdf(count=2)
        image = self.root / "picture.tiff"
        frames = [Image.new("RGB", (30, 20), color) for color in ("red", "blue")]
        frames[0].save(image, save_all=True, append_images=frames[1:])
        for frame in frames:
            frame.close()
        originals = {path: hashlib.sha256(path.read_bytes()).digest() for path in (first, image)}
        result = self.run_tool((first, image), "tool:pdf_merge")
        self.assertEqual(result.completed, 2)
        self.assertEqual([file.state for file in result.files], ["done", "done"])
        doc = PdfReader(result.outputs[0])
        self.assertEqual(len(doc.pages), 4)
        self.assertIn("PAGE 1", doc.pages[0].extract_text())
        self.assertIn("PAGE 2", doc.pages[1].extract_text())
        self.assertTrue(doc.pages[0].get("/Annots"))
        with fitz.open(result.outputs[0]) as rendered:
            self.assertGreater(rendered[2].get_pixmap().samples[0], 200)
            self.assertGreater(rendered[3].get_pixmap().samples[2], 200)
        self.assertEqual(originals, {path: hashlib.sha256(path.read_bytes()).digest() for path in originals})

    def test_pdf_page_selection_split_and_rotation_preserve_objects_and_order(self):
        source = self.pdf()
        selected = self.run_tool(source, "tool:pdf_select", ToolOptions(pdf_pages="3,1-2,2")).outputs[0]
        self.assertEqual([page.extract_text().strip() for page in PdfReader(selected).pages], ["PAGE 3", "PAGE 1", "PAGE 2"])
        split = self.run_tool(source, "tool:pdf_split", ToolOptions(pdf_pages="3-2")).outputs[0]
        outputs = sorted(split.glob("*.pdf"))
        self.assertEqual(len(outputs), 2)
        self.assertEqual([PdfReader(path).pages[0].extract_text().strip() for path in outputs], ["PAGE 3", "PAGE 2"])
        rotated = self.run_tool(source, "tool:pdf_rotate", ToolOptions(pdf_pages="2", rotation=90)).outputs[0]
        reader = PdfReader(rotated)
        self.assertEqual([page.rotation for page in reader.pages], [0, 90, 0])
        self.assertTrue(reader.pages[1].get("/Annots"))
        with fitz.open(source) as before, fitz.open(rotated) as after:
            self.assertEqual(after[1].rect.width, before[1].rect.height)
            self.assertEqual(after[1].rect.height, before[1].rect.width)

    def test_pdf_compression_preserves_rendered_pixels_and_never_increases_size(self):
        source = self.pdf()
        output = self.run_tool(source, "tool:pdf_compress").outputs[0]
        self.assertLessEqual(output.stat().st_size, source.stat().st_size)
        with fitz.open(source) as before, fitz.open(output) as after:
            self.assertEqual(len(before), len(after))
            for first, second in zip(before, after):
                self.assertEqual(first.get_pixmap().samples, second.get_pixmap().samples)

    def test_invalid_pages_and_encrypted_merge_leave_no_partial_output(self):
        source = self.pdf()
        failed = self.run_tool(source, "tool:pdf_split", ToolOptions(pdf_pages="9"), fail=True)
        self.assertEqual(failed.files[0].state, "failed")
        encrypted = self.root / "encrypted.pdf"
        writer = PdfWriter()
        writer.add_blank_page(72, 72)
        writer.encrypt("password")
        writer.write(encrypted)
        writer.close()
        failed = self.run_tool((source, encrypted), "tool:pdf_merge", fail=True)
        self.assertEqual([file.state for file in failed.files], ["failed", "failed"])
        self.assertFalse(list(self.root.glob("*_converted*")))
        self.assertFalse(list(self.root.glob(".magic*")))

    def test_cancel_a_file_in_combined_merge_cancels_the_whole_atomic_merge(self):
        first, second = self.pdf("first.pdf"), self.pdf("second.pdf")
        service, results, entered = ConversionService(), [], threading.Event()
        service.completed.connect(results.append)
        def blocked(*args):
            entered.set()
            while True:
                check_cancelled()
                time.sleep(0.005)
        with patch("magic_converter.core.conversion.builtin_converters.runtime_catalog", return_value=self.catalog), patch(
                "magic_converter.core.jobs.combined_worker.execute", side_effect=blocked):
            service.submit((first, second), "tool:pdf_merge")
            pump_until(entered.is_set)
            self.assertTrue(service.cancel_file(1, 2))
            pump_until(lambda: bool(results))
        self.assertEqual([file.state for file in results[0].files], ["cancelled", "cancelled"])
        self.assertFalse(list(self.root.glob("*_converted*")))
        service.pool.waitForDone(3000)
        service.deleteLater()

    def test_combined_merge_reports_one_fraction_for_all_its_inputs(self):
        first, second = self.pdf("first.pdf"), self.pdf("second.pdf")
        service, results, updates = ConversionService(), [], []
        service.completed.connect(results.append)
        service.progress_changed.connect(updates.append)
        with patch("magic_converter.core.conversion.builtin_converters.runtime_catalog", return_value=self.catalog):
            service.submit((first, second), "tool:pdf_merge")
            pump_until(lambda: bool(results))
        self.assertTrue(any(update.overall_percent is not None and 0 < update.overall_percent < 100 for update in updates))
        self.assertEqual(updates[-1].overall_percent, 100)
        self.assertEqual(updates[-1].finished_files, 2)
        service.pool.waitForDone(3000)
        service.deleteLater()

    def test_image_tools_transform_actual_pixels_and_keep_alpha(self):
        source = self.root / "pixels.png"
        image = Image.new("RGBA", (3, 2))
        image.putdata([(255, 0, 0, 10), (0, 255, 0, 20), (0, 0, 255, 30),
                       (255, 255, 0, 40), (0, 255, 255, 50), (255, 0, 255, 60)])
        image.save(source)
        for key, options, expected in (
            ("tool:image_rotate", ToolOptions(rotation=90), image.transpose(Image.Transpose.ROTATE_270)),
            ("tool:image_flip", ToolOptions(flip="vertical"), image.transpose(Image.Transpose.FLIP_TOP_BOTTOM)),
            ("tool:image_crop", ToolOptions(crop_x=1, crop_width=2, crop_height=1), image.crop((1, 0, 3, 1)))):
            with self.subTest(key=key):
                output = self.run_tool(source, key, options).outputs[0]
                with Image.open(output) as actual:
                    self.assertEqual(actual.size, expected.size)
                    self.assertEqual(actual.convert("RGBA").tobytes(), expected.tobytes())
                expected.close()
        grayscale = self.run_tool(source, "tool:image_grayscale").outputs[0]
        with Image.open(grayscale) as actual:
            for original, changed in zip(image.getdata(), actual.convert("RGBA").getdata()):
                self.assertEqual(changed[0], changed[1])
                self.assertEqual(changed[1], changed[2])
                self.assertEqual(changed[3], original[3])
        image.close()

    def test_animated_image_frames_and_timing_survive_transform_and_extraction(self):
        source = self.root / "animated.gif"
        frames = [Image.new("RGB", (16, 10), color) for color in ("red", "green", "blue")]
        frames[0].save(source, save_all=True, append_images=frames[1:], duration=[100, 200, 300], loop=2)
        for frame in frames:
            frame.close()
        output = self.run_tool(source, "tool:image_rotate").outputs[0]
        with Image.open(output) as actual:
            self.assertEqual(actual.n_frames, 3)
            self.assertEqual(actual.size, (10, 16))
            self.assertEqual(actual.info["loop"], 2)
            self.assertEqual([frame.info["duration"] for frame in ImageSequence.Iterator(actual)], [100, 200, 300])
        extracted = self.run_tool(source, "tool:image_frames", ToolOptions(frame_interval=2)).outputs[0]
        manifest = json.loads((extracted / "frames.json").read_text())
        self.assertEqual([frame["source_frame"] for frame in manifest["frames"]], [1, 3])
        self.assertEqual([frame["start_ms"] for frame in manifest["frames"]], [0, 300])
        self.assertEqual([frame["duration_ms"] for frame in manifest["frames"]], [100, 300])
        with Image.open(extracted / manifest["frames"][1]["file"]) as frame:
            self.assertEqual(frame.getpixel((0, 0))[:3], (0, 0, 255))

    def test_apng_poster_and_animation_are_preserved_and_bad_crop_rolls_back(self):
        source = self.root / "poster.png"
        frames = [Image.new("RGBA", (10, 6), color) for color in ("red", "green", "blue")]
        frames[0].save(source, save_all=True, append_images=frames[1:], default_image=True, duration=[80, 120], loop=1)
        for frame in frames:
            frame.close()
        output = self.run_tool(source, "tool:image_flip").outputs[0]
        with Image.open(output) as decoded:
            self.assertTrue(decoded.info["default_image"])
            self.assertEqual(decoded.n_frames, 3)
            self.assertEqual(decoded.info["loop"], 1)
        failed = self.run_tool(source, "tool:image_crop", ToolOptions(crop_x=999), fail=True)
        self.assertEqual(failed.files[0].state, "failed")
        self.assertEqual(len(list(self.root.glob("*_converted*"))), 1)

    def test_identical_tiff_pages_remain_separate_frames(self):
        source = self.root / "identical.tiff"
        frames = [Image.new("RGBA", (8, 6), "red") for _ in range(3)]
        frames[0].save(source, save_all=True, append_images=frames[1:])
        for frame in frames:
            frame.close()
        output = self.run_tool(source, "tool:image_flip").outputs[0]
        with Image.open(output) as actual:
            self.assertEqual(actual.n_frames, 3)
            self.assertEqual([frame.getpixel((0, 0)) for frame in ImageSequence.Iterator(actual)], [(255, 0, 0, 255)] * 3)
        failed = self.run_tool(source, "tool:image_frames", ToolOptions(max_frames=1), fail=True)
        self.assertEqual(failed.files[0].state, "failed")
        self.assertEqual(len(list(self.root.glob("*_converted*"))), 1)

    def test_fractional_apng_frame_durations_are_not_rounded_to_integers(self):
        source = self.root / "fractional.png"
        frames = [Image.new("RGBA", (10, 8), color) for color in ("red", "blue")]
        frames[0].save(source, save_all=True, append_images=frames[1:], duration=[16.5, 33.5], loop=0)
        for frame in frames:
            frame.close()
        output = self.run_tool(source, "tool:image_rotate").outputs[0]
        with Image.open(output) as actual:
            self.assertEqual([frame.info["duration"] for frame in ImageSequence.Iterator(actual)], [16.5, 33.5])

    def test_subtitle_shifts_preserve_vtt_settings_ids_and_inline_times(self):
        source = self.root / "captions.vtt"
        source.write_text("WEBVTT\n\nSTYLE\n::cue { color: lime; }\n\nNOTE a note\n\nintro\n00:01.000 --> 00:03.000 line:10% align:start\n<v Speaker>첫 줄 <00:02.000>둘째 줄\nline two\n", encoding="utf-8")
        output = self.run_tool(source, "tool:subtitle_shift", ToolOptions(subtitle_offset_ms=1250)).outputs[0]
        cues, headers = read_subtitle(output)
        self.assertEqual((cues[0].start_ms, cues[0].end_ms), (2250, 4250))
        self.assertEqual(cues[0].identifier, "intro")
        self.assertEqual(cues[0].settings, "line:10% align:start")
        self.assertIn("<00:00:03.250>", cues[0].text)
        self.assertIn("line two", cues[0].text)
        self.assertTrue(any(block.startswith("STYLE") for block in headers))
        self.assertTrue(any(block.startswith("NOTE") for block in headers))

    def test_negative_subtitle_shift_clamps_or_drops_cues_with_a_warning(self):
        source = self.root / "captions.srt"
        source.write_bytes("1\n00:00:00,100 --> 00:00:00,500\n초기\n\n2\n00:00:00,800 --> 00:00:02,000\n다음\n".encode("cp949"))
        result = self.run_tool(source, "tool:subtitle_shift", ToolOptions(subtitle_offset_ms=-1000))
        cues, _ = read_subtitle(result.outputs[0])
        self.assertEqual((cues[0].start_ms, cues[0].end_ms, cues[0].text), (0, 1000, "다음"))
        self.assertTrue(result.warnings)
        failed = self.run_tool(source, "tool:subtitle_shift", ToolOptions(subtitle_offset_ms=-9999), fail=True)
        self.assertEqual(failed.files[0].state, "failed")

    def test_subtitle_conversion_rejects_invalid_times_instead_of_silently_dropping(self):
        for time_line in ("00:99:00,000 --> 00:00:02,000", "00:00:03,000 --> 00:00:01,000", "invalid --> value"):
            source = self.root / "bad.srt"
            source.write_text("1\n" + time_line + "\nbody\n", encoding="utf-8")
            self.assertEqual(self.run_tool(source, "tool:subtitle_shift", fail=True).files[0].state, "failed")
        self.assertFalse(list(self.root.glob("*_converted*")))

    def archive(self, name="archive.zip", entries=None):
        path = self.root / name
        with zipfile.ZipFile(path, "w") as packed:
            for key, value in (entries or {"empty/": b"", "folder/한글.txt": "내용".encode(), "binary.bin": bytes(range(256))}).items():
                packed.writestr(key, value)
        return path

    def test_archives_extract_real_unicode_binary_and_empty_directory_contents(self):
        original = self.archive()
        inputs = [original]
        from magic_converter.core.archives.archive_converter import convert_archive
        for suffix in (".tar", ".tar.gz"):
            path = self.root / ("converted" + suffix)
            convert_archive(original, path, suffix)
            inputs.append(path)
        for source in inputs:
            with self.subTest(source=source):
                output = self.run_tool(source, "tool:archive_extract").outputs[0]
                self.assertTrue((output / "empty").is_dir())
                self.assertEqual((output / "folder" / "한글.txt").read_bytes(), "내용".encode())
                self.assertEqual((output / "binary.bin").read_bytes(), bytes(range(256)))
        single = self.root / "single.bin.gz"
        with gzip.open(single, "wb") as stream:
            stream.write(b"lone gzip payload")
        output = self.run_tool(single, "tool:archive_extract").outputs[0]
        self.assertEqual((output / "single.bin").read_bytes(), b"lone gzip payload")

    def test_archive_rejects_traversal_windows_names_duplicates_and_case_aliases(self):
        cases = [{name: b"bad"} for name in ("../escape.txt", "/absolute", "C:/drive.txt", "folder/../escape", "CON.txt", "file. ", "bad:stream")]
        cases += [{"Name.txt": b"a", "name.txt": b"b"}, {"Folder/a": b"a", "folder/b": b"b"}]
        for index, entries in enumerate(cases):
            with self.subTest(entries=entries):
                source = self.archive(f"bad{index}.zip", entries)
                failed = self.run_tool(source, "tool:archive_extract", fail=True)
                self.assertEqual(failed.files[0].state, "failed")
        self.assertFalse(list(self.root.glob("*_converted*")))
        self.assertFalse((self.root.parent / "escape.txt").exists())

    def test_an_empty_archive_publishes_an_empty_result_folder(self):
        source = self.root / "empty.zip"
        with zipfile.ZipFile(source, "w"):
            pass
        result = self.run_tool(source, "tool:archive_extract")
        self.assertTrue(result.outputs[0].is_dir())
        self.assertEqual(list(result.outputs[0].iterdir()), [])

    def test_archive_rejects_links_devices_and_corrupt_payloads(self):
        path = self.root / "link.zip"
        info = zipfile.ZipInfo("link")
        info.create_system = 3
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr(info, "../outside")
        self.assertEqual(self.run_tool(path, "tool:archive_extract", fail=True).files[0].state, "failed")
        for kind in (tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.FIFOTYPE):
            path = self.root / f"kind{kind.decode()}.tar"
            with tarfile.open(path, "w") as archive:
                info = tarfile.TarInfo("link")
                info.type, info.linkname = kind, "../outside"
                archive.addfile(info)
            self.assertEqual(self.run_tool(path, "tool:archive_extract", fail=True).files[0].state, "failed")
        corrupt = self.archive("corrupt.zip", {"data.txt": b"known payload"})
        corrupt.write_bytes(corrupt.read_bytes().replace(b"known payload", b"other payload"))
        self.assertEqual(self.run_tool(corrupt, "tool:archive_extract", fail=True).files[0].state, "failed")
        self.assertFalse(list(self.root.glob("*_converted*")))

    def test_archive_limits_and_cancellation_remove_the_whole_staged_folder(self):
        source = self.archive(entries={"one.txt": b"12345", "two.txt": b"67890"})
        for limit in ({"MAX_ARCHIVE_ENTRIES": 1}, {"MAX_ARCHIVE_BYTES": 4}):
            program = '''
import sys
from magic_converter.core.archives import archive_safety
from magic_converter.core.jobs.specialized_worker import helper_main
for key,value in LIMIT.items():setattr(archive_safety,key,value)
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
'''.replace("LIMIT", repr(limit))
            with patch("magic_converter.core.jobs.specialized_worker.worker_command", side_effect=lambda a,b: [sys.executable,"-c",program,str(a),str(b)]):
                self.assertEqual(self.run_tool(source, "tool:archive_extract", fail=True).files[0].state, "failed")
        from magic_converter.core.archives.archive_converter import extract_archive
        from magic_converter.core.conversion.converter import publish_folder
        event = threading.Event()
        token = current.set(Control(event, lambda _: None))
        try:
            with self.assertRaises(JobCancelled):
                publish_folder(source, lambda folder: extract_archive(source, folder, lambda _: event.set()))
        finally:
            current.reset(token)
        self.assertFalse(list(self.root.glob("*_converted*")))
        self.assertFalse(list(self.root.glob(".magic*")))

    def test_old_settings_migrate_and_tool_options_roundtrip_through_ui(self):
        legacy = default_preferences().to_dict()
        legacy["version"] = 1
        for recipe in legacy["presets"]:
            recipe.pop("tools", None)
        path = self.root / "settings.json"
        write_json(path, legacy)
        store = SettingsStore(path)
        preferences = store.load()
        self.assertEqual(read_json(path)["version"], SCHEMA_VERSION)
        self.assertEqual(preferences, default_preferences())
        self.assertTrue(store.backup.is_file())
        from magic_converter.ui.settings.settings_dialog import SettingsDialog
        from magic_converter.ui.settings.tool_options import ToolOptionsDialog
        recipe = Preset("reverse-pages", "역순 추출", "tool:pdf_select", tools=ToolOptions(pdf_pages="3,1"))
        preferences = replace(preferences, presets=(*preferences.presets, recipe))
        dialog = SettingsDialog(preferences.base_values(), preferences=preferences)
        dialog.editor.preset_list.setCurrentRow(len(preferences.presets) - 1)
        self.assertTrue(dialog.editor.tool_button.isEnabled())
        self.assertEqual(dialog.values()["preferences"], preferences)
        options_dialog = ToolOptionsDialog("tool:pdf_select", recipe.tools)
        options_dialog.fields["pdf_pages"].setText("wrong")
        options_dialog.accept()
        self.assertTrue(options_dialog.error.text())
        options_dialog.fields["pdf_pages"].setText("2-1")
        options_dialog.accept()
        self.assertEqual(options_dialog.result(), QDialog.DialogCode.Accepted)
        self.assertEqual(options_dialog.value().pdf_pages, "2-1")
        options_dialog.deleteLater()
        dialog.deleteLater()

    def test_old_settings_stay_active_if_upgrading_the_file_is_not_writable(self):
        legacy = default_preferences(threshold=29).to_dict()
        legacy["version"] = 1
        path = self.root / "settings.json"
        write_json(path, legacy)
        before = path.read_bytes()
        store = SettingsStore(path)
        with patch.object(store, "save", side_effect=PermissionError("locked")):
            preferences = store.load()
        self.assertEqual(preferences.threshold, 29)
        self.assertTrue(store.read_only)
        self.assertEqual(path.read_bytes(), before)
        self.assertFalse(list(self.root.glob("*.corrupt-*")))


@unittest.skipUnless(find_ffmpeg(), "FFmpeg unavailable")
class MediaToolTests(ToolTestCase):
    def setUp(self):
        super().setUp()
        self.ffmpeg = find_ffmpeg()
        inputs = self.root / "input-frames"
        inputs.mkdir()
        for index in range(12):
            Image.new("RGB", (48, 32), (index * 20, 0, 220 - index * 15)).save(inputs / f"frame_{index:03d}.png")
        self.source = self.root / "source.mp4"
        result = run_process([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-framerate", "10",
            "-i", str(inputs / "frame_%03d.png"), "-f", "lavfi", "-i", "sine=frequency=440:duration=1.2",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(self.source)], timeout=10)
        self.assertEqual(result.returncode, 0)

    def decode(self, source):
        result = run_process([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(source), "-f", "null", "-"], timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_trim_and_mute_have_correct_streams_duration_and_decode_successfully(self):
        original = hashlib.sha256(self.source.read_bytes()).digest()
        trimmed = self.run_tool(self.source, "tool:media_trim", ToolOptions(start_ms=400, duration_ms=500)).outputs[0]
        info = probe_media(trimmed, self.ffmpeg)
        self.assertAlmostEqual(float(info["format"]["duration"]), 0.5, delta=0.12)
        video = next(stream for stream in info["streams"] if stream["codec_type"] == "video")
        self.assertEqual(int(video["nb_frames"]), 5)
        self.decode(trimmed)
        muted = self.run_tool(self.source, "tool:media_mute").outputs[0]
        self.assertEqual({stream["codec_type"] for stream in probe_media(muted, self.ffmpeg)["streams"]}, {"video"})
        self.decode(muted)
        self.assertEqual(hashlib.sha256(self.source.read_bytes()).digest(), original)

    def test_frame_interval_and_segment_match_the_actual_decoded_video_frames(self):
        output = self.run_tool(self.source, "tool:media_frames", ToolOptions(start_ms=200, duration_ms=600, frame_interval=2)).outputs[0]
        manifest = json.loads((output / "frames.json").read_text())
        self.assertEqual(len(manifest["frames"]), 3)
        self.assertEqual((manifest["width"], manifest["height"]), (48, 32))
        for name, number in zip(manifest["frames"], (2, 4, 6)):
            with Image.open(output / name) as frame:
                color = frame.getpixel((10, 10))
                self.assertAlmostEqual(color[0], number * 20, delta=8)
                self.assertAlmostEqual(color[2], 220 - number * 15, delta=8)

    def test_frame_limit_and_invalid_segment_publish_no_partial_folder_or_file(self):
        self.assertEqual(self.run_tool(self.source, "tool:media_frames", ToolOptions(max_frames=2), fail=True).files[0].state, "failed")
        self.assertEqual(self.run_tool(self.source, "tool:media_trim", ToolOptions(start_ms=5000), fail=True).files[0].state, "failed")
        self.assertFalse(list(self.root.glob("*_converted*")))

    def test_audio_trim_keeps_pcm_samples_and_has_no_video_stream(self):
        source = self.root / "audio.wav"
        result = run_process([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-i", str(self.source), "-vn", "-c:a", "pcm_s16le", str(source)], timeout=10)
        self.assertEqual(result.returncode, 0)
        output = self.run_tool(source, "tool:media_trim", ToolOptions(start_ms=200, duration_ms=600)).outputs[0]
        with wave.open(str(output)) as audio:
            self.assertEqual(audio.getnframes(), round(audio.getframerate() * 0.6))
            self.assertTrue(audio.readframes(100))
        self.assertEqual({stream["codec_type"] for stream in probe_media(output, self.ffmpeg)["streams"]}, {"audio"})
        self.decode(output)


if __name__ == "__main__":
    unittest.main()
