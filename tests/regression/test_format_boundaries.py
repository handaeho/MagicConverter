"""Regression cases from the Filee parity audit, including negative gates."""

import copy
import hashlib
import io
import os
import shutil
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from PIL import Image

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.specialized.data_converter import encode_data, read_data
from magic_converter.core.specialized.ebook_converter import (
    Book,
    Chapter,
    FB,
    OPF,
    XHTML,
    read_book,
    write_epub,
)
from magic_converter.core.specialized.geo_converter import read_geo, write_geo
from magic_converter.core.automation.automation_store import WatchRule
from magic_converter.core.settings.preferences import Preset
from magic_converter.core.automation.watch_engine import WatchEngine, digest_file
from magic_converter.core.diagnostics.document_diagnostics import (
    DocumentCheckError,
    check_documents,
    validate_document_report,
)
from magic_converter.core.engines.rhwp_engine import locate
from magic_converter.core.specialized.safe_html import image_uri
from tools.validation.verify_license_checkout import verify


class Fixtures(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def file(self, name, text):
        path = self.root / name
        path.write_text(text, encoding="utf-8")
        return path


class DataRegressionTests(Fixtures):
    def test_magic_data_ordinary_xml_never_becomes_null(self):
        for raw in (
            '<magic-data version="1"><customer id="42">Alice</customer> tail</magic-data>',
            '<magic-data version="1" customer="42">null</magic-data>',
            '<magic-data version="1"/>',
            '<magic-data version="1">ordinary text</magic-data>',
            '<magic-data version="2">null</magic-data>',
        ):
            with self.subTest(raw=raw):
                source = self.file("source.xml", raw)
                value = read_data(source, "xml")
                self.assertIn("$xml", value)
                restored = ET.fromstring(encode_data(value, "xml"))
                original = ET.fromstring(raw)
                self.assertEqual(ET.tostring(restored), ET.tostring(original))

    def test_existing_typed_xml_roundtrips_including_explicit_null(self):
        for value in (None, "", "null", 42, False, [None, {"한글": [1, "Alice"]}]):
            with self.subTest(value=value):
                source = self.file("source.xml", encode_data(value, "xml"))
                self.assertEqual(read_data(source, "xml"), value)

    def kml(self, extended, schema="", name=""):
        return self.file(
            "source.kml",
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>'
            + schema
            + "<Placemark>"
            + name
            + "<ExtendedData>"
            + extended
            + "</ExtendedData><Point><coordinates>127,37</coordinates></Point></Placemark></Document></kml>",
        )

    def test_kml_simple_data_is_preserved_with_and_without_local_schema(self):
        extended = '<SchemaData schemaUrl="#people"><SimpleData name="population">42</SimpleData><SimpleData name="active">true</SimpleData></SchemaData>'
        schema = '<Schema id="people"><SimpleField name="population" type="int"/><SimpleField name="active" type="bool"/></Schema>'
        features = read_geo(self.kml(extended, schema), "kml")
        self.assertEqual(features[0]["properties"], {"population": 42, "active": True})
        self.assertIs(type(features[0]["properties"]["population"]), int)
        output = self.root / "roundtrip.kmz"
        write_geo(features, output, "kmz")
        self.assertEqual(read_geo(output, "kmz"), features)
        notices = []
        without = read_geo(self.kml(extended), "kml", notices.append)
        self.assertEqual(
            without[0]["properties"], {"population": "42", "active": "true"}
        )
        self.assertTrue(any("SimpleData" in n for n in notices))

    def test_kml_external_schema_is_not_fetched_and_values_remain_text(self):
        notices = []
        source = self.kml(
            '<SchemaData schemaUrl="https://127.0.0.1:1/schema.kml#s"><SimpleData name="code">0042</SimpleData></SchemaData>'
        )
        with patch("socket.socket", side_effect=AssertionError("network attempted")):
            result = read_geo(source, "kml", notices.append)
        self.assertEqual(result[0]["properties"], {"code": "0042"})
        self.assertTrue(any("SimpleData" in n for n in notices))

    def test_kml_duplicate_invalid_typed_and_unsupported_data_are_refused(self):
        cases = [
            (
                '<Data name="name"><value>overwrite</value></Data>',
                "",
                "<name>original</name>",
            ),
            (
                '<Data name="n"><value>a</value></Data><SchemaData><SimpleData name="n">b</SimpleData></SchemaData>',
                "",
                "",
            ),
            (
                '<SchemaData schemaUrl="#s"><SimpleData name="n">2147483648</SimpleData></SchemaData>',
                '<Schema id="s"><SimpleField name="n" type="int"/></Schema>',
                "",
            ),
            (
                '<SchemaData schemaUrl="#s"><SimpleData name="n">yes</SimpleData></SchemaData>',
                '<Schema id="s"><SimpleField name="n" type="bool"/></Schema>',
                "",
            ),
            ('<SchemaData><SimpleArrayData name="n"/></SchemaData>', "", ""),
        ]
        for extra, schema, name in cases:
            with self.subTest(extra=extra), self.assertRaises(ConversionError):
                read_geo(self.kml(extra, schema, name), "kml")


class EpubRegressionTests(Fixtures):
    def test_fb2_multiline_heading_keeps_inline_markup_and_valid_phrasing(self):
        source = self.file(
            "source.fb2",
            f'<FictionBook xmlns="{FB}"><description><title-info><book-title>Book</book-title><lang>en</lang></title-info></description><body><section><title><p>One <emphasis>italic</emphasis> tail</p><p>Two <strong>bold</strong></p></title><p>Body</p></section></body></FictionBook>',
        )
        book = read_book(source, "fb2")
        output = self.root / "result.epub"
        write_epub(book, output)
        with zipfile.ZipFile(output) as archive:
            root = ET.fromstring(archive.read("EPUB/chapter0.xhtml"))
        heading = root.find(f".//{{{XHTML}}}h2")
        self.assertIsNotNone(heading)
        self.assertEqual("".join(heading.itertext()), "One italic tailTwo bold")
        self.assertEqual(len(heading.findall(f"{{{XHTML}}}br")), 2)
        self.assertEqual(len(heading.findall(f"{{{XHTML}}}em")), 1)
        self.assertFalse(any(n.tag == f"{{{XHTML}}}p" for n in heading.iter()))

    def test_bmp_cover_and_body_use_one_lossless_png_resource(self):
        image = Image.new("RGB", (7, 5), (22, 87, 210))
        image.putpixel((0, 0), (240, 17, 6))
        raw = io.BytesIO()
        image.save(raw, "BMP")
        uri = image_uri(raw.getvalue())
        book = Book(
            "Bitmap",
            language="en",
            cover=uri,
            chapters=[Chapter("One", f'<p><img src="{uri}"/><img src="{uri}"/></p>')],
        )
        notices = []
        output = self.root / "result.epub"
        write_epub(book, output, notices.append)
        with zipfile.ZipFile(output) as archive:
            opf = ET.fromstring(archive.read("EPUB/package.opf"))
            images = [
                n
                for n in opf.iter(f"{{{OPF}}}item")
                if n.get("media-type", "").startswith("image/")
            ]
            self.assertEqual(len(images), 1)
            self.assertEqual(images[0].get("media-type"), "image/png")
            self.assertEqual(images[0].get("properties"), "cover-image")
            href = images[0].get("href")
            with Image.open(io.BytesIO(archive.read("EPUB/" + href))) as result:
                self.assertEqual(result.convert("RGB").tobytes(), image.tobytes())
            chapter = ET.fromstring(archive.read("EPUB/chapter0.xhtml"))
            self.assertEqual(
                [n.get("src") for n in chapter.iter(f"{{{XHTML}}}img")], [href, href]
            )
        self.assertEqual(len(notices), 1)
        self.assertEqual(read_book(output, "epub").title, "Bitmap")


class WatchRegressionTests(Fixtures):
    def setUp(self):
        super().setUp()
        self.input = self.root / "in"
        self.input.mkdir()
        self.source = self.input / "data.json"
        self.source.write_bytes(b'{"x":1}')
        self.rule = WatchRule(
            "r",
            str(self.input),
            str(self.root / "out"),
            Preset("yaml", "YAML", "data:yaml"),
            settle=0.1,
        )
        self.engine = WatchEngine(self.root / "state", (self.rule,), rehash_interval=2)

    def complete(self, event):
        output = Path(event["work"]) / "results" / "result.yaml"
        output.write_text("x: 1", encoding="utf-8")
        self.assertIsNotNone(self.engine.finish(event, True, [output]))
        self.engine.cleanup(event)

    def first(self):
        self.assertEqual(self.engine.scan(0), [])
        event = self.engine.scan(0.2)[0][1]
        self.complete(event)
        return event

    def rewrite_same_stat(self, value):
        stat = self.source.stat()
        self.source.write_bytes(value)
        os.utime(self.source, ns=(stat.st_atime_ns, stat.st_mtime_ns))

    def test_same_size_mtime_change_is_detected_without_restart_and_settles(self):
        original = self.first()
        self.rewrite_same_stat(b'{"x":2}')
        self.assertEqual(self.engine.scan(0.4), [])
        self.assertEqual(self.engine.scan(2.3), [])  # detected, settling
        self.assertEqual(self.engine.scan(2.35), [])
        event = self.engine.scan(2.5)[0][1]
        self.assertNotEqual(event["key"], original["key"])
        self.assertEqual(event["digest"], hashlib.sha256(b'{"x":2}').hexdigest())
        self.complete(event)
        self.assertEqual(self.engine.scan(5), [])
        restarted = WatchEngine(self.root / "state", (self.rule,))
        restarted.scan(0)
        self.assertEqual(restarted.scan(0.2), [])

    def test_same_stat_content_must_still_be_stable_at_confirmation(self):
        self.first()
        self.rewrite_same_stat(b'{"x":2}')
        self.engine.scan(2.3)
        self.rewrite_same_stat(b'{"x":3}')
        self.assertEqual(self.engine.scan(2.5), [])
        event = self.engine.scan(2.7)[0][1]
        self.assertEqual(event["digest"], hashlib.sha256(b'{"x":3}').hexdigest())

    def test_unchanged_content_rehashes_periodically_without_poll_hashing_or_duplicates(
        self,
    ):
        self.first()
        with patch(
            "magic_converter.core.automation.watch_engine.digest_file", wraps=digest_file
        ) as hashed:
            for now in (0.4, 0.8, 1.2, 1.6, 2.3, 2.5, 3, 3.5, 4, 4.4):
                self.assertEqual(self.engine.scan(now), [])
            self.assertEqual(hashed.call_count, 2)
        os.utime(self.source, None)
        self.assertEqual(self.engine.scan(5), [])
        self.assertEqual(self.engine.scan(5.2), [])


class PackagingRegressionTests(unittest.TestCase):
    @unittest.skipUnless(
        shutil.which("git"), "Git required for real checkout verification"
    )
    def test_git_crlf_and_lf_checkouts_keep_all_upstream_license_hashes(self):
        result = verify()
        self.assertEqual(result["status"], "passed", result)
        self.assertEqual([len(c["files"]) for c in result["checkouts"]], [7, 7])

    @unittest.skipUnless(
        shutil.which("git"), "Git required for real checkout verification"
    )
    def test_checkout_gate_detects_missing_byte_preservation_rule(self):
        root = Path(__file__).resolve().parents[2]
        with tempfile.TemporaryDirectory() as temp:
            candidate = Path(temp)
            shutil.copytree(root / "licenses/stage7", candidate / "licenses/stage7")
            (candidate / ".gitattributes").write_text(
                "# No original-byte rule\n", encoding="utf-8"
            )
            result = verify(candidate)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(
            any(
                not file["passed"]
                for item in result["checkouts"]
                for file in item["files"]
            )
        )


@unittest.skipUnless(locate(), "Bundled rhwp required for real document diagnostics")
class NativeGateRegressionTests(Fixtures):
    def native_patches(self):
        pass
        pass

    def test_native_errors_fail_with_all_subtest_details_retained(self):
        self.native_patches()
        with patch(
            "magic_converter.core.engines.hwp_converter.run_com",
            side_effect=RuntimeError("Injected native failure"),
        ) as helper:
            with self.assertRaises(DocumentCheckError) as raised:
                check_documents(self.root, test_native=True)
        report = raised.exception.report
        self.assertEqual(helper.call_count, 4)
        self.assertEqual(report["status"], "failed")
        self.assertEqual(len(report["validation_errors"]), 4)
        self.assertTrue(
            all(
                "Injected native failure" in n["error"]
                for n in report["native"].values()
            )
        )

    def test_structural_loss_and_missing_comparisons_cannot_pass(self):
        self.native_patches()
        from magic_converter.core.documents.hwp_documents import (
            DocumentData,
            Paragraph,
            TextRun,
            write_docx,
        )

        def export(source, destination, operation):
            if destination.suffix == ".docx":
                write_docx(
                    DocumentData([Paragraph([TextRun("Missing objects")])]), destination
                )
            else:
                shutil.copy2(
                    self.root / ("rhwp-result" + destination.suffix), destination
                )

        with patch("magic_converter.core.engines.hwp_converter.run_com", side_effect=export):
            with self.assertRaises(DocumentCheckError) as raised:
                check_documents(self.root, test_native=True)
        report = raised.exception.report
        self.assertTrue(report["native"][".docx"]["differences"])
        self.assertTrue(any("native .docx" in n for n in report["validation_errors"]))
        for mutate in (
            lambda r: r["native"].pop(".hwp"),
            lambda r: r["native"][".hwpx"].pop("differences"),
            lambda r: r["native"][".pdf"].pop("pdf"),
        ):
            candidate = copy.deepcopy(report)
            candidate["native"][".docx"]["differences"] = []
            mutate(candidate)
            with self.assertRaises(DocumentCheckError):
                validate_document_report(candidate)

    def test_explicitly_unavailable_or_disabled_native_check_is_a_valid_skip(self):
        pass
        self.assertFalse(result["native_required"])
        self.assertEqual(result["native"]["status"], "skipped")
        self.assertEqual(result["status"], "passed")
        with self.assertRaises(DocumentCheckError):
            validate_document_report({"status": "passed"})


if __name__ == "__main__":
    unittest.main()
