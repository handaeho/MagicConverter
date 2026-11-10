"""Image declarations and scoped anchors must survive real book round trips."""

import base64
import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag
from xml.etree import ElementTree as ET

from PIL import Image
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.specialized.ebook_converter import (
    Book,
    Chapter,
    FB,
    XHTML,
    read_book,
    write_epub,
)
from magic_converter.core.specialized.safe_html import decode_image, image_uri
from magic_converter.core.specialized.safe_html import sanitize
from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.conversion.conversion_execution import execute_file


class EpubIntegrityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def image(self, kind="PNG"):
        stream = io.BytesIO()
        Image.new("RGB", (7, 5), "blue").save(stream, kind)
        return stream.getvalue()

    def test_wrong_image_mime_and_non_data_prefix_are_refused_without_output(self):
        for mime, kind in (("png", "BMP"), ("jpeg", "PNG")):
            uri = (
                "data:image/"
                + mime
                + ";base64,"
                + base64.b64encode(self.image(kind)).decode()
            )
            with self.subTest(mime=mime), self.assertRaisesRegex(
                ConversionError, "MIME"
            ):
                decode_image(uri)
            source = self.root / (mime + ".md")
            source.write_text(f'<p><img src="{uri}"/></p>', encoding="utf-8")
            request = prepare_request([source], "document:epub", create_catalog())
            with self.assertRaises(ConversionError):
                execute_file(
                    request, request.files[0], None, lambda _: None, lambda _: None
                )
            self.assertEqual(list(self.root.glob("*.epub")), [])
        with self.assertRaises(ConversionError):
            decode_image(image_uri(self.image()).replace("data:", "fake:"))

    def test_duplicate_or_invalid_ids_and_missing_targets_are_refused(self):
        bodies = (
            '<p id="same">A</p><p id="same">B</p>',
            '<p id="has space">A</p>',
            '<p><a href="#missing">Link</a></p>',
            '<a href="chapter9.xhtml#missing">Link</a>',
        )
        for body in bodies:
            with self.subTest(body=body), self.assertRaises(ConversionError):
                write_epub(
                    Book("Book", chapters=[Chapter("One", body)]),
                    self.root / "out.epub",
                )
        self.assertFalse((self.root / "out.epub").exists())

    def validate_links(self, path):
        with zipfile.ZipFile(path) as archive:
            documents = {
                name: ET.fromstring(archive.read(name))
                for name in archive.namelist()
                if name.startswith("EPUB/chapter")
            }
        links = []
        for name, document in documents.items():
            ids = [n.get("id") for n in document.iter() if n.get("id")]
            self.assertEqual(len(ids), len(set(ids)))
            for anchor in document.iter(f"{{{XHTML}}}a"):
                href = anchor.get("href", "")
                if not href or ":" in href:
                    continue
                file, fragment = urldefrag(href)
                target = "EPUB/" + file if file else name
                self.assertIn(target, documents)
                if fragment:
                    self.assertIn(
                        unquote(fragment),
                        {n.get("id") for n in documents[target].iter()},
                    )
                links.append((name, target, fragment))
        return links

    def test_fb2_notes_and_unicode_anchors_survive_two_epub_roundtrips(self):
        source = self.root / "notes.fb2"
        source.write_text(
            f'<FictionBook xmlns="{FB}" xmlns:l="http://www.w3.org/1999/xlink"><description><title-info><book-title>Notes</book-title><lang>en</lang></title-info></description><body><section id="main"><title id="title"><p>Main</p></title><p><a l:href="#한글">note</a></p></section></body><body name="notes"><section id="한글"><p id="start">Note <a l:href="#main">back</a></p></section></body></FictionBook>',
            encoding="utf-8",
        )
        book = read_book(source, "fb2")
        for generation in range(3):
            output = self.root / f"roundtrip-{generation}.epub"
            write_epub(book, output)
            links = self.validate_links(output)
            self.assertEqual(len(links), 2)
            self.assertTrue(all(start != target for start, target, _ in links))
            book = read_book(output, "epub")
            self.assertIn("Note", book.chapters[1].body)

    def test_same_id_in_separate_chapters_is_valid_and_local_links_stay_local(self):
        book = Book(
            "Book",
            chapters=[
                Chapter("One", '<p id="x">One <a href="#x">self</a></p>'),
                Chapter("Two", '<p id="x">Two <a href="#x">self</a></p>'),
            ],
        )
        output = self.root / "local.epub"
        write_epub(book, output)
        self.assertTrue(
            all(source == target for source, target, _ in self.validate_links(output))
        )
        book.chapters.append(Chapter("Three", '<a href="#x">ambiguous</a>'))
        with self.assertRaisesRegex(ConversionError, "모호"):
            write_epub(book, self.root / "ambiguous.epub")

    def test_inert_container_anchors_survive_real_pipeline_roundtrips(self):
        for tag in ("div", "section", "aside", "article", "main", "figure", "custom"):
            with self.subTest(tag=tag):
                source = self.root / (tag + ".epub")
                write_epub(Book("Containers", language="en", chapters=[Chapter(
                    "One", '<p><a href="#note">jump</a></p>'
                    '<div id="note"><p>kept</p></div>')]), source)
                with zipfile.ZipFile(source) as archive:
                    contents = [(i, archive.read(i)) for i in archive.infolist()]
                with zipfile.ZipFile(source, "w") as archive:
                    for info, data in contents:
                        if info.filename == "EPUB/chapter0.xhtml":
                            doc = ET.fromstring(data)
                            next(n for n in doc.iter() if n.get("id") == "note").tag = f"{{{XHTML}}}{tag}"
                            data = ET.tostring(doc, encoding="utf-8", xml_declaration=True)
                        archive.writestr(info, data)
                for _ in range(3):
                    request = prepare_request([source], "document:epub", create_catalog())
                    source = execute_file(request, request.files[0], None, lambda _: None, lambda _: None)
                    self.assertEqual(len(self.validate_links(source)), 1)
                    self.assertIn("kept", read_book(source, "epub").chapters[0].body)

    def test_literal_percent_unicode_and_cross_chapter_links_survive(self):
        identities = ("p%20q", "p%2520q", "한글%23", "literal%", "a#b", "plain")
        for identity in identities:
            with self.subTest(identity=identity):
                fragment = quote(identity, safe="")
                book = Book("Fragments", language="en", chapters=[
                    Chapter("One", f'<p id="{identity}">one <a href="#{fragment}">self</a>'
                            f'<a href="chapter1.xhtml#{fragment}">next</a></p>'),
                    Chapter("Two", f'<p id="{identity}">two <a href="chapter0.xhtml#{fragment}">back</a></p>')])
                for generation in range(4):
                    output = self.root / f"percent-{identities.index(identity)}-{generation}.epub"
                    write_epub(book, output)
                    links = self.validate_links(output)
                    self.assertEqual(len(links), 3)
                    self.assertEqual(sum(a != b for a, b, _ in links), 2)
                    book = read_book(output, "epub")

    def test_removed_active_content_never_retains_an_anchor(self):
        for tag in ("script", "style", "iframe", "object", "svg", "math", "template", "head", "video", "audio"):
            with self.subTest(tag=tag):
                clean = sanitize(f'<{tag} id="unsafe">discard</{tag}><p>kept</p>')
                self.assertNotIn("unsafe", clean)
                self.assertNotIn("discard", clean)
                with self.assertRaises(ConversionError):
                    write_epub(Book("Unsafe", chapters=[Chapter("One", clean + '<a href="#unsafe">link</a>')]), self.root / "unsafe.epub")


if __name__ == "__main__":
    unittest.main()
