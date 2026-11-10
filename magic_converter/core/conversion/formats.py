"""Format identities and aliases, without importing encoders or Qt."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True)
class FileFormat:
    id: str
    label: str
    category: str
    extensions: tuple[str, ...]
    readable: bool = True
    writable: bool = True

    @property
    def suffix(self) -> str:
        return "." + self.extensions[0]


class FormatRegistry:
    def __init__(self, formats: Iterable[FileFormat]) -> None:
        self._formats: dict[str, FileFormat] = {}
        self._extensions: dict[str, FileFormat] = {}
        for item in formats:
            if not item.id or item.id != item.id.lower() or not item.extensions:
                raise ValueError("A format needs a lowercase id and extensions")
            if item.id in self._formats:
                raise ValueError(f"Duplicate format: {item.id}")
            for extension in item.extensions:
                if not extension or extension.startswith(".") or extension != extension.lower():
                    raise ValueError(f"Invalid extension: {extension}")
                if extension in self._extensions:
                    raise ValueError(f"Duplicate extension: {extension}")
                self._extensions[extension] = item
            self._formats[item.id] = item
        self._suffixes = tuple(sorted(self._extensions, key=len, reverse=True))

    @property
    def known(self) -> tuple[FileFormat, ...]:
        return tuple(self._formats.values())

    def get(self, format_id: str) -> FileFormat:
        return self._formats[format_id.lower()]

    def detect(self, path: str | Path) -> FileFormat | None:
        name = Path(path).name.lower()
        return next((self._extensions[ext] for ext in self._suffixes
                     if name.endswith("." + ext)), None)

    def ids(self, category: str) -> frozenset[str]:
        return frozenset(item.id for item in self.known if item.category == category)

    def extensions(self, category: str) -> frozenset[str]:
        return frozenset("." + ext for item in self.known if item.category == category
                         and item.readable for ext in item.extensions)


from magic_converter.core.specialized.special_formats import FORMAT_SPECS

FORMATS = FormatRegistry((
    FileFormat("png", "PNG", "image", ("png",)),
    FileFormat("jpg", "JPG", "image", ("jpg", "jpeg")),
    FileFormat("webp", "WebP", "image", ("webp",)),
    FileFormat("bmp", "BMP", "image", ("bmp",)),
    FileFormat("tiff", "TIFF", "image", ("tiff", "tif")),
    FileFormat("heic", "HEIC", "image", ("heic", "heif")),
    FileFormat("avif", "AVIF", "image", ("avif",)),
    FileFormat("svg", "SVG", "image", ("svg",), writable=False),
    *(FileFormat(ext, ext.upper(), "video", (ext,))
      for ext in ("mp4", "mov", "mkv", "webm", "avi", "wmv", "gif")),
    *(FileFormat(ext, ext.upper(), "audio", (ext,))
      for ext in ("mp3", "m4a", "wav", "flac", "ogg", "opus", "wma")),
    FileFormat("aiff", "AIFF", "audio", ("aiff", "aif")),
    FileFormat("pdf", "PDF", "pdf", ("pdf",)),
    FileFormat("docx", "DOCX", "document", ("docx",)),
    FileFormat("md", "Markdown", "document", ("md", "markdown")),
    FileFormat("txt", "TXT", "document", ("txt",)),
    FileFormat("epub", "EPUB", "document", ("epub",)),
    *(FileFormat(ext, ext.upper(), "document", (ext,), writable=False)
      for ext in ("rtf", "rtfd", "textclipping")),
    FileFormat("html", "HTML", "document", ("html",), readable=False),
    *(FileFormat(ext, ext.upper(), "hwp", (ext,)) for ext in ("hwp", "hwpx")),
    FileFormat("ppt", "PPT", "slide", ("ppt",), writable=False),
    *(FileFormat(ext, ext.upper(), "document", (ext,), writable=False) for ext in ("docm", "dotx", "dotm")),
    FileFormat("pptx", "PPTX", "slide", ("pptx",)),
    *(FileFormat(ext, ext.upper(), "slide", (ext,), writable=False) for ext in ("pptm", "potx", "potm", "ppsx", "ppsm")),
    *(FileFormat(ext, ext.upper(), "sheet", (ext,), writable=False) for ext in ("xlsm", "xltx", "xltm")),
    *(FileFormat(ext, ext.upper(), "sheet", (ext,)) for ext in ("xlsx", "ods", "csv", "tsv")),
    FileFormat("xls", "XLS", "sheet", ("xls",), writable=False),
    *(FileFormat(ext, ext.upper(), "subtitle", (ext,)) for ext in ("srt", "vtt")),
    *(FileFormat(ext, ext.upper(), "archive", (ext,)) for ext in ("zip", "tar")),
    FileFormat("gz", "GZIP", "archive", ("gz", "gzip"), writable=False),
    FileFormat("tar.gz", "TAR.GZ", "archive", ("tar.gz", "tgz")),
    FileFormat("rar", "RAR", "archive", ("rar",), writable=False),
    *(FileFormat(*spec) for spec in FORMAT_SPECS),
))

IMAGE_EXTENSIONS = FORMATS.extensions("image")
VIDEO_EXTENSIONS = FORMATS.extensions("video")
AUDIO_EXTENSIONS = FORMATS.extensions("audio")
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | AUDIO_EXTENSIONS
DOCUMENT_EXTENSIONS = FORMATS.extensions("document")
HWP_EXTENSIONS = FORMATS.extensions("hwp")
SLIDE_EXTENSIONS = FORMATS.extensions("slide")
SHEET_EXTENSIONS = FORMATS.extensions("sheet")
ARCHIVE_EXTENSIONS = FORMATS.extensions("archive")
