"""Bounded input readers for structured formats and embedded assets."""

import math
import posixpath
import zipfile
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from defusedxml import ElementTree as SafeET
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs.job_control import check_cancelled

MAX_FILE = 64 * 1024 * 1024
MAX_PACKAGE = 256 * 1024 * 1024


def read_bytes(path: Path) -> bytes:
    if path.stat().st_size > MAX_FILE:
        raise ConversionError("이 형식은 파일당 64 MiB까지 처리합니다.")
    with path.open("rb") as stream:
        data = stream.read(MAX_FILE + 1)
    if len(data) > MAX_FILE:
        raise ConversionError("입력 크기 제한을 초과했습니다.")
    check_cancelled()
    return data


def text(path: Path) -> str:
    data = read_bytes(path)
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16")
    try:
        return data.decode("utf-8-sig")
    except UnicodeDecodeError:
        return data.decode("cp949")


def xml(data: bytes):
    if len(data) > MAX_FILE:
        raise ConversionError("XML 크기 제한을 초과했습니다.")
    root = SafeET.fromstring(
        data, forbid_dtd=True, forbid_entities=True, forbid_external=True
    )
    pending, count = [(root, 0)], 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if depth > 64 or count > 500000:
            raise ConversionError("XML 중첩 깊이/항목 제한을 초과했습니다.")
        if count % 1024 == 0:
            check_cancelled()
        pending.extend((c, depth + 1) for c in node)
    return root


def safe_name(name: str) -> str:
    name = name.replace("\\", "/")
    p = PurePosixPath(name)
    if (
        not name
        or name.startswith("/")
        or ":" in name
        or any(x in ("..", ".") for x in name.split("/"))
    ):
        raise ConversionError("패키지에 안전하지 않은 경로가 있습니다.")
    return str(p)


class Package:
    def __init__(self, path):
        self.zip = zipfile.ZipFile(path)
        try:
            entries = self.zip.infolist()
            if len(entries) > 5000 or sum(e.file_size for e in entries) > MAX_PACKAGE:
                raise ConversionError(
                    "전자책/지리 패키지 크기 또는 항목 제한을 초과했습니다."
                )
            self.names = {}
            seen = set()
            for e in entries:
                if e.is_dir():
                    continue
                name = safe_name(e.filename)
                if (
                    name.casefold() in seen
                    or e.file_size > MAX_FILE
                    or (e.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    raise ConversionError(
                        "중복 경로·심볼릭 링크 또는 너무 큰 패키지 항목입니다."
                    )
                seen.add(name.casefold())
                self.names[name] = e
        except BaseException:
            self.zip.close()
            raise

    def read(self, name):
        check_cancelled()
        with self.zip.open(self.names[safe_name(name)]) as stream:
            data = stream.read(MAX_FILE + 1)
        if len(data) > MAX_FILE:
            raise ConversionError("패키지 항목 크기 제한을 초과했습니다.")
        return data

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.zip.close()


def relative_asset(base, href):
    parsed = urlsplit(href)
    if parsed.scheme or parsed.netloc:
        raise ConversionError("외부 리소스는 전자책에 포함할 수 없습니다.")
    return safe_name(
        posixpath.normpath(
            posixpath.join(posixpath.dirname(base), unquote(parsed.path))
        )
    )


def finite(value):
    result = float(value)
    if not math.isfinite(result):
        raise ConversionError("무한대/NaN 좌표는 변환할 수 없습니다.")
    return result
