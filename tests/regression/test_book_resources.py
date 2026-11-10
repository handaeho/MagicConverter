"""B05: repeated images stay references; budgets refuse without publication."""

import io
import random
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from bs4 import BeautifulSoup
from PIL import Image
from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_execution import execute_file
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.specialized.ebook_converter import Book, Chapter, read_book, write_epub
from magic_converter.core.specialized.safe_html import image_uri, decode_image, sanitize
from magic_converter.core.specialized.book_assets import BookLimitError, ImageAssets
from magic_converter.core.specialized.special_io import Package


class BookResourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        stream = io.BytesIO()
        Image.frombytes("RGB", (128, 128), random.Random(42).randbytes(128 * 128 * 3)).save(stream, "PNG")
        self.image = stream.getvalue()

    def fixture(self, repeats=32):
        path = self.root / "repeated.epub"
        uri = image_uri(self.image)
        write_epub(Book("Repeated", language="en", cover=uri, chapters=[
            Chapter("One", ('<p><img src="' + uri + '"/></p>') * repeats),
            Chapter("Two", '<p><img src="' + uri + '"/></p>')]), path)
        return path

    def test_repeated_asset_is_read_once_across_cover_and_chapters(self):
        source = self.fixture()
        original = Package.read
        images = []

        def read(package, name):
            if "/images/" in name:
                images.append(name)
            return original(package, name)

        with patch.object(Package, "read", read):
            book = read_book(source, "epub")
        body_bytes = sum(len(ch.body.encode("utf-8")) for ch in book.chapters)
        self.assertLess(body_bytes, len(self.image), "Repeated binary expanded into the text model")
        self.assertEqual(len(images), 1)
        self.assertEqual(len(book.assets), 1)
        self.assertEqual(book.assets.byte_size, len(self.image))
        refs = [i["src"] for ch in book.chapters for i in BeautifulSoup(ch.body, "html.parser").find_all("img")]
        self.assertEqual(len(refs), 33)
        self.assertEqual(set(refs), {book.cover})
        self.assertEqual(book.assets.resolve(book.cover)[0], self.image)

    def test_refs_are_internal_and_all_exported_images_keep_original_bytes(self):
        from docx import Document
        source = self.fixture(3)
        for target in ("html", "md", "fb2", "docx", "epub", "txt"):
            with self.subTest(target=target):
                request = prepare_request([source], "document:" + target if target != "fb2" else "ebook:fb2", create_catalog())
                path = execute_file(request, request.files[0], None, lambda _: None, lambda _: None)
                if target in {"html", "md"}:
                    output = path.read_text(encoding="utf-8")
                    self.assertNotIn("mc-asset:", output)
                    images = BeautifulSoup(output, "html.parser").find_all("img")
                    self.assertEqual(len(images), 4)
                    self.assertTrue(all(decode_image(n["src"])[0] == self.image for n in images))
                elif target in {"epub", "fb2"}:
                    book = read_book(path, target)
                    self.assertEqual(book.assets.resolve(book.cover)[0], self.image)
                elif target == "docx":
                    self.assertEqual(len(Document(path).inline_shapes), 4)
                    with zipfile.ZipFile(path) as archive:
                        self.assertEqual([archive.read(n) for n in archive.namelist() if n.startswith("word/media/")], [self.image])

    def test_model_budget_and_missing_ref_fail_and_active_content_stays_removed(self):
        store = ImageAssets(max_bytes=len(self.image) + 8)
        ref = store.add(self.image)
        self.assertEqual(store.add(self.image), ref)
        with self.assertRaises(BookLimitError):
            store.add_text("x" * 9)
        with self.assertRaisesRegex(Exception, "참조"):
            store.resolve("mc-asset:missing")
        self.assertNotIn("unsafe", sanitize('<script id="unsafe">bad</script><p>kept</p>', assets=store))

    def test_expanded_export_budget_refuses_without_any_output(self):
        import sys
        from magic_converter.core.jobs import specialized_worker as worker

        program = """
import sys
from magic_converter.core.specialized import book_assets
from magic_converter.core.jobs.specialized_worker import helper_main
book_assets.MAX_OUTPUT_BYTES=1024
raise SystemExit(helper_main(sys.argv[1],sys.argv[2]))
"""
        source = self.fixture()
        for target in ("html", "md", "epub", "fb2"):
            key = "ebook:fb2" if target == "fb2" else "document:" + target
            request = prepare_request([source], key, create_catalog())
            with self.subTest(target=target), patch.object(worker, "worker_command", side_effect=lambda a, b: [sys.executable, "-c", program, str(a), str(b)]), self.assertRaises(BookLimitError):
                execute_file(request, request.files[0], None, lambda _: None, lambda _: None)
            self.assertEqual(sorted(self.root.iterdir()), [source])


if __name__ == "__main__":
    unittest.main()
