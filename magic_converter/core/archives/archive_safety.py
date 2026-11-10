"""Limits and Windows-safe archive member identities shared by all writers."""

from pathlib import PurePosixPath
import re

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs.job_control import check_cancelled

MAX_ARCHIVE_ENTRIES = 100000
MAX_ARCHIVE_BYTES = 4 * 1024 ** 3
DISK_RESERVE = 64 * 1024 ** 2


def safe_member(name: str) -> str:
    normalized = name.replace("\\", "/")
    path = PurePosixPath(normalized)
    if (not normalized or path.is_absolute() or ".." in path.parts or not path.parts
            or any(re.search(r'[<>:"|?*\x00-\x1f]', part) or part.endswith((".", " "))
                   or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", part) for part in path.parts)):
        raise ConversionError(f"안전하지 않은 압축 내부 경로: {name}")
    return str(path)


def validate_members(members):
    names, components = set(), {}
    total = count = 0
    for name, size, stream in members:
        check_cancelled()
        count += 1
        total += size
        if count > MAX_ARCHIVE_ENTRIES or size < 0 or total > MAX_ARCHIVE_BYTES:
            raise ConversionError("압축 항목 수 또는 해제 크기 제한(100,000개 / 4 GiB)을 초과했습니다.")
        name = safe_member(name)
        key = name.casefold()
        if key in names:
            raise ConversionError(f"압축 안에 중복된 이름이 있습니다: {name}")
        names.add(key)
        parts = PurePosixPath(name).parts
        for index in range(1, len(parts) + 1):
            component = "/".join(parts[:index])
            identity = component.casefold()
            if identity in components and components[identity] != component:
                raise ConversionError(f"대소문자가 충돌하는 압축 경로입니다: {name}")
            components[identity] = component
        yield name, size, stream


def copy_member(source, target, expected):
    total = 0
    while True:
        check_cancelled()
        chunk = source.read(min(1024 * 1024, expected - total + 1))
        if not chunk:
            break
        total += len(chunk)
        if total > expected:
            raise ConversionError("압축 해제 데이터가 선언된 크기를 초과했습니다.")
        target.write(chunk)
    if total != expected:
        raise ConversionError("압축 항목의 실제 크기가 올바르지 않습니다.")
