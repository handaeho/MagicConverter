"""Deduplicated, validated book images and logical model/output byte budgets."""

import base64
import hashlib

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.specialized.safe_html import _image_mime, decode_image

MAX_MODEL_BYTES = 256 * 1024 * 1024
MAX_OUTPUT_BYTES = 256 * 1024 * 1024
ASSET_PREFIX = "mc-asset:"


class BookLimitError(ConversionError):
    pass


def text_size(value, maximum):
    if len(value) > maximum:
        raise BookLimitError("전자책 모델/출력 확장 용량 제한을 초과했습니다.")
    return len(value.encode("utf-8"))


class ImageAssets:
    def __init__(self, max_bytes=None):
        self.max_bytes = MAX_MODEL_BYTES if max_bytes is None else max_bytes
        self.byte_size = self.text_bytes = 0
        self._images = {}
        self._uris = {}

    def __len__(self):
        return len(self._images)

    def _check(self, size):
        check_cancelled()
        if self.byte_size + self.text_bytes + size > self.max_bytes:
            raise BookLimitError("전자책 모델 확장 용량 제한을 초과했습니다.")

    def add_text(self, text):
        size = text_size(text, self.max_bytes)
        self._check(size)
        self.text_bytes += size

    def check_model(self, book):
        size = 0
        for value in (book.title, *book.authors, book.language, book.description, book.publisher):
            size += text_size(value, self.max_bytes)
        for chapter in book.chapters:
            check_cancelled()
            size += text_size(chapter.title, self.max_bytes) + text_size(chapter.body, self.max_bytes)
        if size + self.byte_size > self.max_bytes:
            raise BookLimitError("전자책 모델 확장 용량 제한을 초과했습니다.")
        self.text_bytes = size

    def _store(self, data, mime):
        ref = ASSET_PREFIX + hashlib.sha256(data).hexdigest()
        if ref not in self._images:
            self._check(len(data))
            self._images[ref] = (data, mime)
            self.byte_size += len(data)
        return ref

    def add(self, data):
        check_cancelled()
        ref = ASSET_PREFIX + hashlib.sha256(data).hexdigest()
        if ref in self._images:
            return ref
        self._check(len(data))
        return self._store(data, _image_mime(data))

    def intern(self, uri):
        if uri.startswith(ASSET_PREFIX):
            self.resolve(uri)
            return uri
        key = hashlib.sha256(uri.encode("utf-8")).hexdigest()
        if key not in self._uris:
            data, mime = decode_image(uri)
            self._uris[key] = self._store(data, mime)
        return self._uris[key]

    def resolve(self, ref):
        check_cancelled()
        if not ref.startswith(ASSET_PREFIX):
            ref = self.intern(ref)
        try:
            return self._images[ref]
        except KeyError as exc:
            raise ConversionError("전자책 모델의 이미지 참조가 없습니다.") from exc

    def uri_size(self, ref):
        data, mime = self.resolve(ref)
        return len("data:" + mime + ";base64,") + 4 * ((len(data) + 2) // 3)

    def data_uri(self, ref):
        data, mime = self.resolve(ref)
        return "data:" + mime + ";base64," + base64.b64encode(data).decode("ascii")


class OutputBudget:
    def __init__(self, max_bytes=None):
        self.max_bytes = MAX_OUTPUT_BYTES if max_bytes is None else max_bytes
        self.used = 0

    def reserve(self, size):
        check_cancelled()
        if self.used + size > self.max_bytes:
            raise BookLimitError("전자책 출력 확장 용량 제한을 초과했습니다.")
        self.used += size

    def write(self, stream, text):
        size = text_size(text, self.max_bytes - self.used)
        self.reserve(size)
        stream.write(text)


def inline_images(book, body, budget):
    """Preflight repeated base64 expansion before serializing a chapter."""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(body, "html.parser")
    images = list(soup.find_all("img", src=True))
    estimate = text_size(body, budget.max_bytes)
    for image in images:
        estimate += book.assets.uri_size(image["src"]) - len(image["src"].encode("utf-8"))
    budget.reserve(estimate)
    cache = {}
    for image in images:
        check_cancelled()
        ref = image["src"]
        if ref not in cache:
            cache[ref] = book.assets.data_uri(ref)
        image["src"] = cache[ref]
    result = str(soup)
    budget.reserve(text_size(result, budget.max_bytes) - estimate)
    return result
