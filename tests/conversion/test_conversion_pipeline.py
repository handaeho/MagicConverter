"""Routing contracts and real output/rollback coverage for the registered pipeline."""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PIL import Image
from docx import Document
from pypdf import PdfReader, PdfWriter
from PySide6.QtWidgets import QApplication

from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_catalog import ConversionEdge, RoutePlanner
from magic_converter.core.conversion.conversion_execution import execute_file, run_legacy_action
from magic_converter.core.conversion.conversion_request import RequestError, prepare_request
from magic_converter.core.conversion.converter import ConversionService, ConversionWorker
from magic_converter.core.conversion.file_router import ALL_ACTIONS, slots_for
from magic_converter.core.conversion.formats import FORMATS, FileFormat, FormatRegistry

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class FormatAndRouteTests(unittest.TestCase):
    def test_aliases_and_compound_archive_extensions_have_one_identity(self):
        for name, expected in (("PHOTO.JPEG", "jpg"), ("photo.HEIF", "heic"),
                               ("sound.AIF", "aiff"), ("notes.MARKDOWN", "md"),
                               ("backup.TAR.GZ", "tar.gz"), ("backup.tgz", "tar.gz"),
                               ("backup.gz", "gz")):
            with self.subTest(name=name):
                self.assertEqual(FORMATS.detect(name).id, expected)
        self.assertIsNone(FORMATS.detect("notpng"))
        self.assertIsNone(FORMATS.detect("image.png.exe"))
        with self.assertRaises(ValueError):
            FormatRegistry((FileFormat("a", "A", "test", ("same",)),
                            FileFormat("b", "B", "test", ("same",))))

    def test_maximum_hops_does_not_discard_a_valid_shorter_prefix(self):
        # The cheapest path to x uses all three hops. A more expensive one-hop
        # prefix must still be available to finish at t within the limit.
        def edge(a, b, cost=1):
            return ConversionEdge(a, b, "test", "test", cost)
        planner = RoutePlanner((edge("a", "b"), edge("b", "c"), edge("c", "x"),
                                edge("a", "x", 4), edge("x", "t")))
        route = planner.plan("a", "t")
        self.assertEqual([(step.source, step.target) for step in route.steps], [("a", "x"), ("x", "t")])
        self.assertEqual(route.cost, 5)
        self.assertIsNone(RoutePlanner((edge("a", "b"), edge("b", "c"), edge("c", "x"),
                                        edge("x", "t"))).plan("a", "t"))

    def test_picture_decks_and_multi_file_outputs_cannot_be_intermediate_formats(self):
        edges = (ConversionEdge("docx", "pdf", "a", "a"),
                 ConversionEdge("pdf", "pptx", "b", "b", direct_only=True),
                 ConversionEdge("pdf", "png", "c", "c", terminal_only=True),
                 ConversionEdge("png", "jpg", "d", "d"))
        planner = RoutePlanner(edges)
        self.assertIsNotNone(planner.plan("pdf", "pptx"))
        self.assertIsNone(planner.plan("docx", "pptx"))
        self.assertIsNotNone(planner.plan("docx", "png"))
        self.assertIsNone(planner.plan("pdf", "jpg"))

    def test_same_format_requires_an_explicit_operation_and_modes_remain_distinct(self):
        planner = RoutePlanner((ConversionEdge("jpg", "png", "a", "a"),
                                ConversionEdge("png", "jpg", "a", "a")))
        self.assertIsNone(planner.plan("jpg", "jpg"))
        catalog = create_catalog()
        for key in ("pptx_image", "pptx_editable"):
            route = catalog.assess("source.pdf", key).route
            self.assertEqual([edge.action_key for edge in route.steps], [key])
            self.assertIsNone(catalog.assess("source.hwp", key).route)
        self.assertEqual(catalog.assess("source.jpg", "strip").route.steps[0].intent, "strip")

    def test_catalog_and_request_construction_do_not_import_qt_or_encoders(self):
        code = """from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import prepare_request
import sys
r = prepare_request(['source.txt'], 'document:png', create_catalog())
assert len(r.files[0].route.steps) == 2
assert not any(n.startswith(('PySide6', 'PIL', 'pymupdf')) for n in sys.modules)
"""
        result = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_missing_engines_have_reasons_without_disabling_builtin_fallbacks(self):
        pass
        pass
        for name, action in (("source.png", "image:jpg"), ("source.hwp", "document:docx"),
                             ("source.docx", "document:hwpx"), ("source.docx", "document:pdf")):
            self.assertIsNotNone(catalog.assess(name, action).route)

    def test_legacy_actions_and_mixed_selection_share_the_registered_paths(self):
        catalog = create_catalog()
        self.assertTrue(all(catalog.policy(key) is not None for key in ALL_ACTIONS))
        request = prepare_request(["photo.png", "report.docx", "photo.png"], "document:pdf", catalog)
        self.assertEqual(len(request.files), 2)
        self.assertEqual([file.route.steps[0].engine_id for file in request.files], ["images", "documents"])
        self.assertTrue(request.uses_office_queue)
        slots = {slot.action.key: slot for slot in slots_for(["photo.png", "report.docx"], "convert")}
        self.assertTrue(slots["document:pdf"].enabled)
        pass
        self.assertFalse(missing["document:pdf"].enabled)
        self.assertIn("PowerPoint", missing["document:pdf"].reason)
        for paths, action in (([], "document:pdf"), (["x.txt"], "unknown"),
                              (["x.txt", "x.unknown"], "document:pdf")):
            with self.assertRaises(RequestError):
                prepare_request(paths, action, catalog)
        self.assertEqual(prepare_request(["x.unknown"], "copy_path", catalog).paths[0].name, "x.unknown")


class PipelineOutputTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        pass

    def tearDown(self):
        self.temporary.cleanup()

    def worker(self, paths, key, *, catalog=None):
        request = prepare_request(paths, key, catalog or self.catalog)
        worker = ConversionWorker(1, request.paths, request.action, None, request=request)
        results = []
        worker.signals.finished.connect(results.append)
        return worker, results

    def test_mixed_image_and_docx_produce_real_pdfs_without_office(self):
        image, document = self.root / "사진.png", self.root / "문서.docx"
        Image.new("RGB", (48, 32), "red").save(image)
        doc = Document()
        doc.add_paragraph("Stage one mixed batch")
        doc.save(document)
        originals = {path: hashlib.sha256(path.read_bytes()).digest() for path in (image, document)}
        worker, results = self.worker((image, document), "document:pdf")
        program = '''
import sys
from magic_converter.core.engines import hwp_converter as h,office_engine
from magic_converter.core.jobs.specialized_worker import helper_main
h.com_available=lambda _:False
office_engine.find_libreoffice=lambda:None
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
'''
        with patch("magic_converter.core.jobs.specialized_worker.worker_command", side_effect=lambda a,b: [sys.executable,"-c",program,str(a),str(b)]):
            worker.run()
        self.assertFalse(results[0].errors)
        self.assertEqual(results[0].completed, 2)
        for output in results[0].outputs:
            self.assertEqual(len(PdfReader(output).pages), 1)
        self.assertEqual(originals, {path: hashlib.sha256(path.read_bytes()).digest() for path in originals})

    def test_real_two_hop_route_publishes_only_final_folder_and_preserves_collision(self):
        source = self.root / "notes.txt"
        source.write_text("Text through PDF into a picture", encoding="utf-8")
        existing = self.root / "notes_converted"
        existing.mkdir()
        (existing / "keep.txt").write_text("keep", encoding="utf-8")
        worker, results = self.worker((source,), "document:png")
        percents = []
        worker.signals.progress.connect(lambda update: percents.append(update.percent) if update.percent is not None else None)
        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", wraps=run_legacy_action) as calls:
            worker.run()
        self.assertFalse(results[0].errors)
        self.assertEqual(calls.call_count, 2)
        workspace = calls.call_args_list[0].args[0].output_source.parent.parent
        self.assertFalse(workspace.exists())
        output = results[0].outputs[0]
        self.assertNotEqual(output, existing)
        self.assertEqual([file.name for file in output.iterdir()], ["page_0001.png"])
        with Image.open(output / "page_0001.png") as image:
            self.assertAlmostEqual(image.info["dpi"][0], 300, delta=0.2)
        self.assertEqual(percents, sorted(percents))
        self.assertEqual(percents[-1], 100)
        self.assertEqual((existing / "keep.txt").read_text(), "keep")
        self.assertEqual({file.name for file in self.root.iterdir()}, {source.name, existing.name, output.name})

    def test_failed_second_hop_cleans_workspace_and_never_publishes_an_intermediate(self):
        source = self.root / "notes.txt"
        source.write_text("Original", encoding="utf-8")
        worker, results = self.worker((source,), "document:png")
        workspaces = []

        def fail(step, progress, notice):
            workspaces.append(step.output_source.parent.parent)
            if step.edge.source == "pdf":
                raise RuntimeError("second hop failed")
            return run_legacy_action(step, progress, notice)

        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=fail), \
                self.assertLogs("magic_converter.core.conversion.converter", level="ERROR"):
            worker.run()
        self.assertEqual(results[0].completed, 0)
        self.assertEqual(len(results[0].errors), 1)
        self.assertIn("second hop failed", results[0].errors[0])
        self.assertEqual(list(self.root.iterdir()), [source])
        self.assertTrue(all(not workspace.exists() for workspace in workspaces))

    def test_cancellation_between_hops_never_publishes_a_result(self):
        source = self.root / "notes.txt"
        source.write_text("Original", encoding="utf-8")
        worker, results = self.worker((source,), "document:png")
        workspaces = []

        def cancel(step, progress, notice):
            output = run_legacy_action(step, progress, notice)
            workspaces.append(step.output_source.parent.parent)
            worker.cancel_event.set()
            return output

        with patch("magic_converter.core.conversion.conversion_execution.run_legacy_action", side_effect=cancel):
            worker.run()
        self.assertTrue(results[0].cancelled)
        self.assertFalse(results[0].errors)
        self.assertEqual(list(self.root.iterdir()), [source])
        self.assertTrue(all(not workspace.exists() for workspace in workspaces))

    def test_pdf_identity_in_a_mixed_batch_is_a_real_copy_and_preserves_source(self):
        source = self.root / "source.pdf"
        writer = PdfWriter()
        writer.add_blank_page(width=72, height=72)
        writer.write(source)
        writer.close()
        data = source.read_bytes()
        request = prepare_request((source,), "document:pdf", self.catalog)
        output = execute_file(request, request.files[0], None, lambda _: None, lambda _: None)
        self.assertNotEqual(output, source)
        self.assertEqual(output.read_bytes(), data)
        self.assertEqual(source.read_bytes(), data)

    def test_service_rejects_unavailable_or_unsupported_batch_before_starting_workers(self):
        service = ConversionService()
        errors = []
        service.failed.connect(errors.append)
        with patch("magic_converter.core.conversion.builtin_converters.runtime_catalog", return_value=self.catalog):
            service.submit((self.root / "video.mp4",), "media:mp3")
            service.submit((self.root / "notes.txt", self.root / "unknown.zzz"), "document:pdf")
        self.assertEqual(len(errors), 2)
        self.assertIn("FFmpeg", errors[0])
        self.assertIn("unknown.zzz", errors[1])
        self.assertEqual(service.active_count, 0)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_an_independently_registered_converter_is_used_by_the_worker(self):
        source = self.root / "source.txt"
        source.write_text("Original", encoding="utf-8")
        base = self.catalog
        engine = next(engine for engine in base.converters if engine.id == "documents")
        called = []

        class CustomConverter:
            id, display_name = "custom", "Custom document engine"
            status, max_parallelism = engine.status, 1
            edges = (ConversionEdge("txt", "md", "custom", "custom", cost=1),)

            def convert(self, step, progress, notice):
                called.append(step.source)
                from magic_converter.core.conversion.converter import output_file
                with output_file(step.source, ".md") as (temporary, destination):
                    temporary.write_text("# Registered engine", encoding="utf-8")
                return destination

        base.register(CustomConverter())
        worker, results = self.worker((source,), "document:md", catalog=base)
        worker.run()
        self.assertEqual(called, [source])
        self.assertFalse(results[0].errors)
        self.assertEqual(results[0].outputs[0].read_text(), "# Registered engine")


if __name__ == "__main__":
    unittest.main()
