"""Stream archive members into a new archive; never extract to the filesystem."""

from __future__ import annotations

import gzip
import io
import os
import stat
import shutil
import tarfile
import zipfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs.job_control import checked, copy_stream, CheckedReader, check_cancelled
from magic_converter.core.archives.archive_safety import safe_member, validate_members, copy_member
from magic_converter.core.archives import archive_safety


@contextmanager
def archive_members(source: Path):
    if source.suffix.lower() == ".zip":
        with zipfile.ZipFile(source) as archive:
            def members():
                for info in checked(archive.infolist()):
                    name = safe_member(info.filename)
                    mode = info.external_attr >> 16
                    if stat.S_IFMT(mode) not in {0, stat.S_IFREG, stat.S_IFDIR}:
                        raise ConversionError("링크/장치 압축 항목은 지원하지 않습니다.")
                    if info.flag_bits & 1:
                        raise ConversionError("암호화 ZIP은 지원하지 않습니다.")
                    if info.is_dir():
                        yield name, 0, None
                    else:
                        with archive.open(info) as stream:
                            yield name, info.file_size, stream
            yield members()
    elif source.suffix.lower() == ".rar":
        import rarfile
        windows_tar = Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32" / "tar.exe"
        if windows_tar.is_file():
            rarfile.BSDTAR_TOOL = str(windows_tar)
        with rarfile.RarFile(source) as archive:
            if archive.needs_password():
                raise ConversionError("암호화 RAR은 지원하지 않습니다.")
            def members():
                for info in checked(archive.infolist()):
                    name = safe_member(info.filename)
                    if info.is_symlink():
                        raise ConversionError("심볼릭 링크 압축 항목은 지원하지 않습니다.")
                    if info.isdir():
                        yield name, 0, None
                    else:
                        with archive.open(info) as stream:
                            yield name, info.file_size, stream
            yield members()
    elif tarfile.is_tarfile(source):
        with tarfile.open(source, "r:*") as archive:
            def members():
                for info in checked(archive):
                    if info.isdir() and info.name in {".", "./"}:
                        continue
                    name = safe_member(info.name)
                    if info.isdir():
                        yield name, 0, None
                    elif info.isfile():
                        stream = archive.extractfile(info)
                        if stream is None:
                            raise ConversionError("압축 항목을 읽지 못했습니다.")
                        with stream:
                            yield name, info.size, stream
                    else:
                        raise ConversionError("링크/장치 압축 항목은 지원하지 않습니다.")
            yield members()
    else:
        # A .gz may wrap one ordinary file, rather than an entire tar stream.
        # spool on disk so large files do not have to fit in RAM.
        import tempfile
        with tempfile.TemporaryFile() as spool, gzip.open(source, "rb") as stream:
            total = 0
            while True:
                check_cancelled()
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > archive_safety.MAX_ARCHIVE_BYTES:
                    raise ConversionError("GZIP 해제 크기가 제한을 초과했습니다.")
                spool.write(chunk)
            size = spool.tell()
            spool.seek(0)
            name = safe_member(source.stem)
            yield iter([(name, size, spool)])


def convert_archive(source: Path, destination: Path, suffix: str) -> None:
    with archive_members(source) as members:
        if suffix == ".zip":
            with zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED, allowZip64=True) as output:
                for name, size, stream in validate_members(members):
                    if stream is None:
                        output.writestr(name.rstrip("/") + "/", b"")
                    else:
                        with output.open(name, "w", force_zip64=True) as target:
                            copy_member(stream, target, size)
        else:
            with tarfile.open(destination, "w:gz" if suffix == ".tar.gz" else "w", format=tarfile.PAX_FORMAT) as output:
                for name, size, stream in validate_members(members):
                    entry = tarfile.TarInfo(name)
                    entry.size = size
                    entry.mode = 0o755 if stream is None else 0o644
                    if stream is None:
                        entry.type = tarfile.DIRTYPE
                    output.addfile(entry, CheckedReader(stream) if stream is not None else None)
    check_cancelled()


def extract_archive(source: Path, folder: Path, progress) -> None:
    # RAR's legacy external streaming reader is not used by this bounded tool.
    if source.suffix.lower() == ".rar":
        raise ConversionError("압축 해제 도구는 ZIP·TAR·GZIP을 지원합니다.")
    with archive_members(source) as members:
        for count, (name, size, stream) in enumerate(validate_members(members), start=1):
            target = folder.joinpath(*name.split("/"))
            if not target.resolve().is_relative_to(folder.resolve()):
                raise ConversionError("압축 경로가 출력 폴더를 벗어났습니다.")
            if stream is None:
                target.mkdir(parents=True, exist_ok=True)
            else:
                if size > max(0, shutil.disk_usage(folder).free - archive_safety.DISK_RESERVE):
                    raise ConversionError("압축 해제에 필요한 디스크 공간이 부족합니다.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("xb") as outgoing:
                    copy_member(stream, outgoing, size)
            progress(None)
    check_cancelled()
