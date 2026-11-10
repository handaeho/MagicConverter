"""Immutable browser drag metadata. No network work while the wheel is open."""
from __future__ import annotations

import base64
import hashlib
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import unquote, urlsplit


def safe_name(name):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", unquote(name)).strip(" .")[:160]
    if not name or name.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "download_" + name
    return name or "download"


@dataclass(frozen=True)
class WebInput:
    name: str
    url: str = ""
    data: bytes = b""

    def __post_init__(self):
        if not self.name or self.name != safe_name(self.name) or bool(self.url) == bool(self.data):
            raise ValueError("웹 입력 이름·데이터가 올바르지 않습니다.")
        if self.url:
            validate_url(self.url)
        if len(self.data) > 32 * 1024 * 1024:
            raise ValueError("드래그 이미지 데이터는 32 MiB까지 지원합니다.")

    @property
    def placeholder(self):
        key = hashlib.sha256(self.url.encode() if self.url else self.data).hexdigest()[:24]
        return Path(tempfile.gettempdir()) / "magic-web-placeholder" / key / self.name


def validate_url(url):
    parsed = urlsplit(url)
    if (len(url) > 16000 or parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or any(ord(c) < 32 for c in url)):
        raise ValueError("웹 파일은 인증 정보가 없는 HTTP/HTTPS 주소로 지정하세요.")
    return parsed


def from_url(url, *, image_hint=False):
    if url.startswith("data:"):
        header, encoded = url.split(",", 1)
        if len(encoded) > 44 * 1024 * 1024 or not re.fullmatch(r"data:image/(png|jpeg|gif|webp);base64", header):
            raise ValueError("지원하는 이미지 data URL이 아닙니다.")
        extension = {"jpeg": "jpg"}.get(header.split("/")[1].split(";")[0], header.split("/")[1].split(";")[0])
        return WebInput("image." + extension, data=base64.b64decode(encoded, validate=True))
    parsed = validate_url(url)
    name = safe_name(parsed.path.rsplit("/", 1)[-1])
    if not Path(name).suffix:
        name += ".jpg" if image_hint else ".bin"
    return WebInput(name, url=url)


def single_text_url(text: str) -> str:
    """A copied address only, never body text or a list of addresses."""
    if not isinstance(text, str) or not 0 < len(text) <= 16000:
        raise ValueError("텍스트 URL의 길이가 올바르지 않습니다.")
    url = text.strip()
    if not url or any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in url):
        raise ValueError("단일 HTTP/HTTPS 주소만 지정하세요.")
    parsed = validate_url(url)
    parsed.port  # Reject malformed/out-of-range ports without changing the URL.
    return url


def from_mime(mime):
    """Return local paths OR immutable web items; mixed drags are refused."""
    from magic_converter.core.conversion.file_router import normalize_paths
    urls = mime.urls() if mime.hasUrls() else []
    if urls and all(url.isLocalFile() for url in urls):
        return normalize_paths(url.toLocalFile() for url in urls), ()
    if any(url.isLocalFile() for url in urls):
        return (), ()
    try:
        from magic_converter.core.automation.browser_mime import adapted, image_selection, WINDOWS
        selection = image_selection(mime)
        blob_pixels = bool(selection and selection.startswith("blob:") and mime.hasImage())
        try:
            explicit = adapted(mime)
        except (ValueError, UnicodeError):
            # Firefox/URLW can repeat a browser-owned blob URI. Its supplied
            # pixels are usable, whereas virtual-file descriptor failures must
            # never be bypassed by a thumbnail.
            if not blob_pixels or mime.hasFormat(WINDOWS.format('FileGroupDescriptorW')):
                raise
            explicit = (_image_input(mime),)
        if explicit is not None:
            items = explicit
        elif blob_pixels:
            items = (_image_input(mime),)
        elif selection:
            items = (from_url(selection, image_hint=True),)
        elif urls and all(url.scheme().lower() in {"http", "https", "data"} for url in urls):
            items = tuple(from_url(url.toString(), image_hint=mime.hasImage() or mime.hasHtml()) for url in urls)
        elif mime.hasImage():
            items = (_image_input(mime),)
        elif mime.hasText() and not mime.hasUrls() and selection is None:
            # Chrome's Copy link address supplies text/plain, without URL MIME.
            # HTML, malformed explicit adapters and URI lists must not fall
            # through to this branch and bypass their stricter checks.
            items = (from_url(single_text_url(mime.text())),)
        else:
            return (), ()
        if not items or len(items) > 100:
            return (), ()
        # Identical URLs are deduplicated like identical local Explorer paths.
        items = tuple(dict.fromkeys(items))
        return tuple(item.placeholder for item in items), items
    except (ValueError, TypeError, KeyError, UnicodeError):
        return (), ()


def _image_input(mime):
    from PySide6.QtCore import QBuffer, QIODevice
    from PySide6.QtGui import QImage
    image = QImage(mime.imageData())
    if image.isNull() or image.width() * image.height() > 40_000_000:
        raise ValueError("Invalid or excessive drag image")
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise ValueError("Unable to encode drag image")
    return WebInput("image.png", data=bytes(buffer.data()))
