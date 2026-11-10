"""Real structures, round trips and refusal/cleanup contracts for stage 7."""

import base64
import hashlib
import io
import json
import os
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import zipfile
from dataclasses import asdict, replace
from email import policy
from email.message import EmailMessage
from pathlib import Path
from xml.etree import ElementTree as ET

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PIL import Image
from bs4 import BeautifulSoup
from PySide6.QtWidgets import QApplication
from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import prepare_request, DropSelection
from magic_converter.core.conversion.conversion_execution import execute_file
from magic_converter.core.conversion.converter import ConversionError, ConversionService
from magic_converter.core.conversion.file_router import slots_for
from magic_converter.core.conversion.formats import FORMATS
from magic_converter.core.settings.preferences import OutputRule, Preset, default_preferences
from magic_converter.core.specialized.data_converter import read_data
from magic_converter.core.jobs.job_control import Control, JobCancelled, JobTimedOut, current
from magic_converter.core.specialized.ebook_converter import (
    Book,
    Chapter,
    write_epub,
    read_book,
    FB,
)
from magic_converter.core.specialized.safe_html import image_uri
from magic_converter.core.diagnostics.special_diagnostics import sample_font, check_specialized
from magic_converter.core.specialized.mesh_converter import read_mesh
from magic_converter.core.specialized.geo_converter import read_geo

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


class SpecialFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.catalog = create_catalog()
        self.notices = []

    def test_data_routes_exclude_incompatible_root_types(self):
        for suffix, key in (
            ("csv", "data:toml"), ("tsv", "data:toml"), ("ndjson", "data:toml"),
            ("toml", "data:jsonl"), ("toml", "data:csv"), ("toml", "data:tsv"),
        ):
            with self.subTest(source=suffix, action=key):
                self.assertIsNone(self.catalog.assess(f"input.{suffix}", key).route)
        self.assertIsNotNone(self.catalog.assess("input.json", "data:toml").route)

    def tearDown(self):
        self.temp.cleanup()

    def file(self, name, content):
        path = self.root / name
        (
            path.write_bytes(content)
            if isinstance(content, bytes)
            else path.write_text(content, encoding="utf-8", newline="")
        )
        return path

    def run_format(self, path, key, output=None):
        request = prepare_request([path], key, self.catalog, output=output)
        return execute_file(
            request, request.files[0], None, lambda _: None, self.notices.append
        )

    def png(self):
        out = io.BytesIO()
        Image.new("RGB", (16, 12), "red").save(out, format="PNG")
        return out.getvalue()

    def book(self):
        path = self.root / "book.epub"
        uri = image_uri(self.png())
        write_epub(
            Book(
                "책",
                ["저자"],
                "ko",
                "설명",
                "출판사",
                [
                    Chapter(
                        "A",
                        f'<h1>제목</h1><p>A<strong>굵게</strong><img src="{uri}"/></p>',
                    ),
                    Chapter(
                        "B",
                        '<p>B</p><table><tr><td colspan="2">합침</td></tr><tr><td>1</td><td>2</td></tr></table>',
                    ),
                ],
                uri,
            ),
            path,
        )
        return path

    def test_frozen_selftest_fixtures_roundtrip_all_six_families(self):
        self.assertEqual(check_specialized(self.root)["status"], "passed")

    def test_data_nested_types_unicode_large_integer_and_jsonl_order(self):
        value = [
            {"한글": 9007199254740993, "nested": [True, None, {"x": -1.25}]},
            {"한글": -4, "nested": []},
        ]
        path = self.file("data.ndjson", "\n".join(json.dumps(v) for v in value))
        for target in ("yaml", "json", "xml"):
            path = self.run_format(path, "data:" + target)
        result = self.run_format(path, "data:jsonl")
        self.assertEqual(
            [
                json.loads(line)
                for line in result.read_text(encoding="utf-8").splitlines()
            ],
            value,
        )

    def test_toml_nested_tables_and_no_null_replacement(self):
        path = self.file(
            "input.json", '{"title":"book","settings":{"enabled":true,"size":42}}'
        )
        toml = self.run_format(path, "data:toml")
        self.assertEqual(read_data(toml, "toml"), json.loads(path.read_text()))
        path.write_text('{"null":null}', encoding="utf-8")
        before = set(self.root.iterdir())
        with self.assertRaises((TypeError, ValueError, ConversionError)):
            self.run_format(path, "data:toml")
        self.assertEqual(set(self.root.iterdir()), before)

    def test_xml_preserves_mixed_content_attributes_namespaces_and_order(self):
        source = self.file(
            "mixed.xml",
            '<x:r xmlns:x="urn:test" x:a="1">before<x:b>inside</x:b>between<x:b/>after</x:r>',
        )
        converted = self.run_format(source, "data:json")
        result = self.run_format(converted, "data:xml")
        a, b = ET.parse(source).getroot(), ET.parse(result).getroot()
        self.assertEqual((a.tag, a.attrib, a.text), (b.tag, b.attrib, b.text))
        self.assertEqual(
            [(e.tag, e.text, e.tail) for e in a], [(e.tag, e.text, e.tail) for e in b]
        )

    def test_duplicate_keys_recursive_yaml_unsafe_tags_dates_and_entities_refused(self):
        cases = (
            ("a.json", '{"a":1,"a":2}'),
            ("a.yaml", "a: 1\na: 2\n"),
            ("cycle.yml", "x: &x [*x]"),
            ("unsafe.yaml", "!!python/object/apply:os.system [echo bad]"),
            ("date.toml", "date = 2026-10-22\n"),
            (
                "xxe.xml",
                '<!DOCTYPE r [<!ENTITY x SYSTEM "file:///C:/Windows/win.ini">]><r>&x;</r>',
            ),
        )
        for name, body in cases:
            path = self.file(name, body)
            with self.subTest(name=name), self.assertRaises(Exception):
                self.run_format(
                    path, "data:json" if not name.endswith("json") else "data:yaml"
                )
            self.assertFalse(list(self.root.glob(".magic_converter-*")))

    def test_data_depth_and_yaml_alias_budget(self):
        for name, body in (
            ("deep.json", "[" * 70 + "0" + "]" * 70),
            ("aliases.yml", "a: &a [1]\nb: [" + ",".join(["*a"] * 101) + "]"),
        ):
            with self.subTest(name=name), self.assertRaises(ConversionError):
                self.run_format(self.file(name, body), "data:xml")

    def test_csv_quoted_newlines_strings_and_nested_refusal(self):
        path = self.file(
            "rows.csv", 'name,note\r\n"한글","line1\nline2"\r\n"=1+2","001"\r\n'
        )
        result = self.run_format(path, "data:json")
        rows = json.loads(result.read_text(encoding="utf-8"))
        self.assertEqual(
            rows,
            [{"name": "한글", "note": "line1\nline2"}, {"name": "=1+2", "note": "001"}],
        )
        back = self.run_format(result, "data:csv")
        self.assertEqual(read_data(back, "csv"), rows)
        bad = self.file("nested.json", '[{"x":{"y":1}}]')
        with self.assertRaises(ConversionError):
            self.run_format(bad, "data:csv")

    def test_font_outlines_names_cmap_fsType_and_fvar_survive_web_containers(self):
        from fontTools.ttLib import TTFont

        path = self.root / "font.ttf"
        sample_font(path)
        for target in ("woff2", "woff", "otf", "ttf"):
            path = self.run_format(path, "font:" + target)
        with TTFont(path) as font:
            self.assertEqual(font["OS/2"].fsType, 2)
            self.assertEqual(font.getBestCmap(), {65: "A", 0xAC00: "ga"})
            self.assertEqual(font["fvar"].axes[0].axisTag, "wght")
            self.assertEqual(
                list(font["glyf"]["A"].getCoordinates(font["glyf"])[0]),
                [(50, 0), (300, 700), (550, 0)],
            )
            self.assertIn("Synthetic verification", font["name"].getDebugName(13))

    def test_cff_font_is_not_renamed_to_ttf(self):
        from fontTools.ttLib import TTFont

        path = self.root / "cff.otf"
        sample_font(path, cff=True)
        web = self.run_format(path, "font:woff2")
        with self.assertRaisesRegex(ConversionError, "CFF"):
            self.run_format(web, "font:ttf")
        restored = self.run_format(web, "font:otf")
        with TTFont(restored) as font:
            self.assertIn("CFF ", font)
            self.assertNotIn("glyf", font)
        self.assertFalse(list(self.root.glob("*converted.ttf")))

    def test_book_package_spine_metadata_images_and_fb2_table(self):
        path = self.book()
        fb2 = self.run_format(path, "ebook:fb2")
        document = ET.parse(fb2).getroot()
        self.assertEqual(len(document.findall(f"{{{FB}}}body/{{{FB}}}section")), 2)
        self.assertEqual(len(list(document.iter(f"{{{FB}}}table"))), 1)
        self.assertFalse(
            any(e.find(f"{{{FB}}}p") is not None for e in document.iter(f"{{{FB}}}p"))
        )
        back = self.run_format(fb2, "document:epub")
        book = read_book(back, "epub")
        self.assertEqual(
            (book.title, book.authors, book.language, book.publisher),
            ("책", ["저자"], "ko", "출판사"),
        )
        with zipfile.ZipFile(back) as z:
            first = z.infolist()[0]
            self.assertEqual(
                (first.filename, first.compress_type), ("mimetype", zipfile.ZIP_STORED)
            )
            self.assertIn(
                self.png(), [z.read(n) for n in z.namelist() if "/images/" in n]
            )
            opf = ET.fromstring(z.read("EPUB/package.opf"))
            spine = opf.find("{http://www.idpf.org/2007/opf}spine")
            self.assertEqual([n.get("idref") for n in spine], ["c0", "c1"])

    def test_book_docx_has_actual_merged_cells_and_picture(self):
        from docx import Document

        doc = Document(self.run_format(self.book(), "document:docx"))
        self.assertEqual((len(doc.tables), len(doc.inline_shapes)), (1, 1))
        self.assertEqual(doc.tables[0].cell(0, 0)._tc.grid_span, 2)
        self.assertEqual(doc.core_properties.title, "책")
        self.assertEqual(doc.core_properties.author, "저자")

    def test_encrypted_traversal_and_duplicate_book_packages_refused(self):
        path = self.book()
        with zipfile.ZipFile(path, "a") as z:
            z.writestr("META-INF/encryption.xml", "<encryption/>")
        with self.assertRaisesRegex(ConversionError, "암호화"):
            self.run_format(path, "document:txt")
        for name in ("../escape.png", "EPUB/../escape.png"):
            path = self.root / "evil.cbz"
            with zipfile.ZipFile(path, "w") as z:
                z.writestr(name, self.png())
            with self.assertRaises(ConversionError):
                self.run_format(path, "document:pdf")
        self.assertFalse((self.root.parent / "escape.png").exists())

    def test_cbz_natural_page_order_pdf_and_epub_image_bytes(self):
        import fitz

        path = self.root / "comic.cbz"
        green = io.BytesIO()
        Image.new("RGB", (20, 10), "green").save(green, format="PNG")
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("10.png", green.getvalue())
            z.writestr("2.png", self.png())
        pdf = self.run_format(path, "document:pdf")
        with fitz.open(pdf) as doc:
            self.assertEqual(len(doc), 2)
            self.assertEqual((doc[0].rect.width, doc[0].rect.height), (12, 9))
            self.assertEqual((doc[1].rect.width, doc[1].rect.height), (15, 7.5))
        epub = self.run_format(path, "document:epub")
        book = read_book(epub, "epub")
        self.assertEqual([c.title for c in book.chapters], ["2.png", "10.png"])
        ref = BeautifulSoup(book.chapters[0].body, "html.parser").find("img")["src"]
        self.assertEqual(book.assets.resolve(ref)[0], self.png())
        cbz = self.run_format(pdf, "ebook:cbz")
        with zipfile.ZipFile(cbz) as z:
            self.assertEqual(z.namelist(), ["00001.png", "00002.png"])

    def mail(self):
        mail = EmailMessage()
        mail["Subject"] = "한글 메일"
        mail["From"] = "a@example.test"
        mail["To"] = "b@example.test"
        mail.set_content("한글 본문", charset="euc-kr")
        mail.add_alternative(
            '<p>한글 <img src="cid:logo" onerror="bad()"/><img src="https://tracker.test/pixel"/></p><script>bad()</script><form><input value="bad"/></form>',
            subtype="html",
        )
        mail.get_payload()[-1].add_related(
            self.png(), maintype="image", subtype="png", cid="<logo>"
        )
        for name in ("../A.bin", "../A.bin"):
            mail.add_attachment(
                b"\x00\xffbinary",
                maintype="application",
                subtype="octet-stream",
                filename=name,
            )
        return self.file("mail.eml", mail.as_bytes(policy=policy.SMTP))

    def test_email_cid_encoded_headers_html_sanitization_and_exact_archive(self):
        path = self.mail()
        output = self.run_format(path, "document:html").read_text(encoding="utf-8")
        self.assertIn("한글 메일", output)
        self.assertIn(image_uri(self.png()), output)
        for bad in ("<script", "onerror", "<input", "https://tracker.test/pixel"):
            self.assertNotIn(bad, output)
        archive = self.run_format(path, "email:attachments")
        with zipfile.ZipFile(archive) as z:
            self.assertEqual(z.read("original.eml"), path.read_bytes())
            manifest = json.loads(z.read("manifest.json"))
            files = [
                e for e in manifest["attachments"] if e["original_name"] == "../A.bin"
            ]
            self.assertEqual(len({e["path"] for e in files}), 2)
            for e in files:
                self.assertEqual(z.read(e["path"]), b"\x00\xffbinary")
                self.assertEqual(
                    e["sha256"], hashlib.sha256(b"\x00\xffbinary").hexdigest()
                )
            self.assertTrue(all(".." not in n.split("/") for n in z.namelist()))

    def test_raw_cp949_mail_header_and_body(self):
        path = self.file(
            "legacy.eml",
            b"Subject: "
            + "한글 제목".encode("cp949")
            + b"\r\nMIME-Version: 1.0\r\nContent-Type: text/plain; charset=cp949\r\nContent-Transfer-Encoding: 8bit\r\n\r\n"
            + "본문 내용".encode("cp949"),
        )
        output = self.run_format(path, "document:txt").read_text(encoding="utf-8")
        self.assertIn("한글 제목", output)
        self.assertIn("본문 내용", output)

    def test_percent_encoded_case_insensitive_cid_is_embedded(self):
        mail = EmailMessage()
        mail["Subject"] = "CID"
        mail.set_content('<p><img src="CID:logo%40EXAMPLE"/></p>', subtype="html")
        mail.add_related(
            self.png(), maintype="image", subtype="png", cid="<logo@example>"
        )
        result = self.run_format(
            self.file("encoded.eml", mail.as_bytes(policy=policy.SMTP)), "document:html"
        )
        self.assertIn(image_uri(self.png()), result.read_text(encoding="utf-8"))

    def test_book_export_metadata_scope_has_explicit_loss_notices(self):
        book = self.book()
        for target, phrase in (
            ("html", "저자·언어·설명·출판사"),
            ("md", "책 메타데이터"),
            ("docx", "출판사와 별도 표지"),
        ):
            self.notices.clear()
            self.run_format(book, "document:" + target)
            self.assertTrue(any(phrase in message for message in self.notices), target)

    def test_mhtml_content_location_inline_resource(self):
        mail = EmailMessage()
        mail.set_type("multipart/related")
        body = EmailMessage()
        body.set_content('<p>saved<img src="image.png"/></p>', subtype="html")
        mail.attach(body)
        image = EmailMessage()
        image.set_content(self.png(), maintype="image", subtype="png")
        image["Content-Location"] = "image.png"
        mail.attach(image)
        path = self.file("saved.mht", mail.as_bytes(policy=policy.SMTP))
        output = self.run_format(path, "document:html").read_text(encoding="utf-8")
        self.assertIn(image_uri(self.png()), output)

    def test_truncated_or_encrypted_mail_is_not_empty_success(self):
        for name, data in (
            (
                "truncated.eml",
                b'Content-Type: multipart/mixed; boundary="x"\r\n\r\n--x\r\nContent-Type: text/plain\r\n\r\ntext',
            ),
            ("encrypted.eml", b"Content-Type: application/pkcs7-mime\r\n\r\nsecret"),
        ):
            with self.subTest(name=name), self.assertRaises(ConversionError):
                self.run_format(self.file(name, data), "document:html")

    def test_concave_obj_negative_indices_preserve_area_and_winding(self):
        obj = self.file(
            "concave.obj",
            "v 0 0 0\nv 2 0 0\nv 2 2 0\nv 1 1 0\nv 0 2 0\nf -5 -4 -3 -2 -1\n",
        )
        ply = self.run_format(obj, "mesh:ply")
        mesh = read_mesh(ply, "ply")
        self.assertEqual(len(mesh.faces), 3)
        areas = []
        for face in mesh.faces:
            a, b, c = [mesh.vertices[i] for i in face]
            areas.append(
                ((b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])) / 2
            )
        self.assertTrue(all(a > 0 for a in areas))
        self.assertEqual(sum(areas), 3)

    def test_binary_big_endian_ply_and_ascii_stl(self):
        header = b"ply\nformat binary_big_endian 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n"
        ply = self.file(
            "binary.ply",
            header + struct.pack(">9fB3i", 0, 0, 0, 1, 0, 0, 0, 1, 0, 3, 0, 1, 2),
        )
        mesh = read_mesh(self.run_format(ply, "mesh:obj"), "obj")
        self.assertEqual(mesh.faces, [(0, 1, 2)])
        stl = self.file(
            "ascii.stl",
            "solid s\nfacet normal 0 0 1\nouter loop\nvertex 0 0 0\nvertex 1 0 0\nvertex 0 1 0\nendloop\nendfacet\nendsolid s\n",
        )
        self.assertEqual(
            len(read_mesh(self.run_format(stl, "mesh:ply"), "ply").faces), 1
        )

    def test_polygon_world_offset_does_not_change_planarity_or_triangulation(self):
        origin = 1_000_000_000
        for planar in (True, False):
            vertices = [(0, 0, 0), (2, 0, 0), (2, 2, 0 if planar else 1), (0, 2, 0)]
            source = self.file(
                f"offset-{planar}.obj",
                "".join(f"v {origin+x} {origin+y} {origin+z}\n" for x, y, z in vertices)
                + "f 1 2 3 4\n",
            )
            if planar:
                mesh = read_mesh(self.run_format(source, "mesh:ply"), "ply")
                self.assertEqual(len(mesh.faces), 2)
            else:
                with self.assertRaisesRegex(ConversionError, "비평면"):
                    self.run_format(source, "mesh:ply")

    def test_mesh_bad_indices_and_nonplanar_polygons_refused(self):
        for name, content in (
            ("bad.obj", "v 0 0 0\nv 1 0 0\nv 0 1 0\nf 0 2 3"),
            ("nonplanar.obj", "v 0 0 0\nv 1 0 0\nv 1 1 1\nv 0 1 0\nf 1 2 3 4"),
        ):
            with self.subTest(name=name), self.assertRaises(ConversionError):
                self.run_format(self.file(name, content), "mesh:stl")
        self.assertFalse(list(self.root.glob("*.stl")))

    def test_geo_polygon_hole_typed_attributes_and_wgs84_precision(self):
        feature = {
            "type": "Feature",
            "properties": {"name": "도형", "nested": {"n": 42}, "yes": True},
            "geometry": {
                "type": "Polygon",
                "coordinates": [
                    [[126, 37], [127, 37], [127, 38], [126, 37]],
                    [[126.2, 37.1], [126.3, 37.1], [126.3, 37.2], [126.2, 37.1]],
                ],
            },
        }
        path = self.file("polygon.geojson", json.dumps(feature))
        kmz = self.run_format(path, "geo:kmz")
        back = self.run_format(kmz, "geo:geojson")
        result = read_geo(back, "geojson")[0]
        self.assertEqual(result, feature)
        with self.assertRaisesRegex(ConversionError, "GPX"):
            self.run_format(path, "geo:gpx")

    def test_gpx_segments_elevation_and_explicit_metadata_loss(self):
        path = self.file(
            "track.gpx",
            '<gpx xmlns="http://www.topografix.com/GPX/1/1" version="1.1"><trk><name>길</name><trkseg><trkpt lat="37" lon="126"><ele>10</ele><time>2026-10-22T00:00:00Z</time></trkpt><trkpt lat="37.1" lon="126.1"/></trkseg><trkseg><trkpt lat="38" lon="127"/><trkpt lat="38.1" lon="127.1"/></trkseg></trk></gpx>',
        )
        result = self.run_format(path, "geo:geojson")
        shape = read_geo(result, "geojson")[0]["geometry"]
        self.assertEqual(shape["type"], "MultiLineString")
        self.assertEqual(len(shape["coordinates"]), 2)
        self.assertEqual(shape["coordinates"][0][0], [126, 37, 10])
        self.assertTrue(any("시각" in n for n in self.notices))

    def test_unknown_crs_or_out_of_range_coordinates_refused(self):
        for extra, coordinate in (
            ({"crs": {"properties": {"name": "EPSG:3857"}}}, [126, 37]),
            ({}, [200000, 400000]),
        ):
            value = {"type": "Point", "coordinates": coordinate, **extra}
            path = self.file("projected.geojson", json.dumps(value))
            with self.assertRaises(ConversionError):
                self.run_format(path, "geo:kml")

    def test_queue_failure_isolated_and_output_rule_collision_preserves_existing(self):
        good = self.file("good.json", '{"x":42}')
        bad = self.file("bad.json", '{"x":')
        service = ConversionService(ffmpeg_path="")
        completed = []
        service.completed.connect(completed.append)
        output = OutputRule("folder", str(self.root / "out"), "{stem}_result")
        service.submit_selection(DropSelection((bad, good), "data:yaml", output=output))
        until = time.monotonic() + 15
        while service.active_count:
            APP.processEvents()
            if time.monotonic() > until:
                service.cancel_all()
                self.fail("queue timed out")
            time.sleep(0.005)
        APP.processEvents()
        service.pool.waitForDone(3000)
        self.assertEqual([f.state for f in completed[0].files], ["failed", "done"])
        result = self.root / "out" / "good_result.yaml"
        self.assertEqual(read_data(result, "yaml"), {"x": 42})
        original = result.read_bytes()
        with self.assertRaises(ConversionError):
            self.run_format(good, "data:yaml", replace(output, collision="error"))
        self.assertEqual(result.read_bytes(), original)
        self.assertFalse(list((self.root / "out").glob(".magic_converter-*")))

    def test_cancel_and_timeout_leave_no_special_outputs(self):
        path = self.file("data.json", '{"x":1}')
        for timed in (False, True):
            event = threading.Event()
            if not timed:
                event.set()
            token = current.set(
                Control(event, lambda _: None, time.monotonic() - 1 if timed else None)
            )
            try:
                with self.assertRaises(JobTimedOut if timed else JobCancelled):
                    self.run_format(path, "data:yaml")
            finally:
                current.reset(token)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_cancellation_after_read_rolls_back_reserved_destination(self):
        path = self.file("rows.csv", "name,value\nrow,42\n")
        request = prepare_request([path], "data:json", self.catalog)
        event = threading.Event()
        token = current.set(Control(event, lambda _: None))
        try:
            with self.assertRaises(JobCancelled):
                execute_file(
                    request,
                    request.files[0],
                    None,
                    lambda _: None,
                    lambda _: event.set(),
                )
        finally:
            current.reset(token)
        self.assertEqual(list(self.root.iterdir()), [path])

    def test_cli_to_and_watch_recipe_resolve_overlapping_targets_per_file(self):
        from magic_converter.cli import select_recipe
        from magic_converter.core.automation.automation_store import WatchRule
        from magic_converter.core.specialized.special_formats import target_action

        for path, to, engine in (
            ("data.json", "csv", "data"),
            ("mail.eml", "zip", "email"),
            ("font.woff2", "otf", "font"),
        ):
            recipe = select_recipe(
                (self.root / path,), default_preferences(), to=to, catalog=self.catalog
            )
            route = self.catalog.assess(self.root / path, recipe.action_key).route
            self.assertEqual(route.steps[-1].engine_id, engine)
            self.assertEqual(
                target_action((self.root / path,), to, self.catalog), recipe.action_key
            )
        rule = WatchRule(
            "r",
            str(self.root),
            str(self.root / "out"),
            Preset("csv", "CSV", "sheet:csv"),
        )
        old = asdict(rule)
        old.pop("target")
        expected = hashlib.sha256(
            json.dumps(old, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()
        self.assertEqual(rule.signature, expected)

    def test_real_cli_watch_mixed_data_and_sheet_target_then_restart(self):
        from openpyxl import Workbook

        folder = self.root / "inbox"
        folder.mkdir()
        (folder / "structured.json").write_text(
            '[{"name":"한글","value":42}]', encoding="utf-8"
        )
        workbook = Workbook()
        workbook.active.append(["name", "value"])
        workbook.active.append(["sheet", 17])
        workbook.save(folder / "table.xlsx")
        for generation in range(2):
            report = self.root / f"watch-{generation}.json"
            result = subprocess.run(
                [
                    sys.executable,
                    str(Path(__file__).resolve().parents[2] / "run_magic_converter.py"),
                    "watch",
                    str(folder),
                    "--to",
                    "csv",
                    "--settle",
                    ".1",
                    "--once",
                    "--data-dir",
                    str(self.root / "profile"),
                    "--report",
                    str(report),
                ],
                capture_output=True,
                timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(
                result.returncode, 0, result.stderr.decode("utf-8", errors="replace")
            )
            data = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual(len(data["jobs"]), 2 if generation == 0 else 0)
            if generation == 0:
                rows = []
                for job in data["jobs"]:
                    published = Path(job["outputs"][0])
                    rows.append(
                        read_data(
                            (
                                published
                                if published.is_file()
                                else next(published.glob("*.csv"))
                            ),
                            "csv",
                        )
                    )
                self.assertIn([{"name": "한글", "value": "42"}], rows)
                self.assertIn([{"name": "sheet", "value": "17"}], rows)

    def test_wheel_visibility_uses_actual_routes_and_aliases(self):
        for name, key in (
            ("a.yml", "data:json"),
            ("a.ndjson", "data:yaml"),
            ("a.ttf", "font:woff2"),
            ("a.fb2", "document:epub"),
            ("a.epub", "ebook:fb2"),
            ("a.mht", "document:html"),
            ("a.obj", "mesh:stl"),
            ("a.geojson", "geo:gpx"),
            ("a.csv", "data:json"),
        ):
            with self.subTest(name=name):
                enabled = {
                    s.action.key for s in slots_for([name], "convert") if s.enabled
                }
                self.assertIn(key, enabled)
                self.assertIsNotNone(self.catalog.assess(Path(name), key).route)
        for name in ("a.msg", "a.eot", "a.dwg", "a.shp", "a.mobi", "a.fbx"):
            self.assertIsNone(FORMATS.detect(name))


if __name__ == "__main__":
    unittest.main()
