"""Bounded HTTP transfer in an owned helper process; failures leave no files."""
from __future__ import annotations

import email.message
import json
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from urllib.parse import urljoin

from magic_converter.core.automation.web_inputs import safe_name, validate_url

MAX_BYTES = 512 * 1024 * 1024
BLOCKED = {".exe", ".com", ".scr", ".dll", ".msi", ".lnk", ".url", ".cmd", ".bat", ".ps1", ".js", ".vbs", ".hta", ".py"}


def identify(path, name, mime=""):
    from magic_converter.core.conversion.formats import FORMATS
    with path.open("rb") as stream:
        head = stream.read(8192)
    if Path(name).suffix.lower() in BLOCKED or head.startswith((b"MZ", b"\x7fELF")):
        raise ValueError("웹 드래그에서 실행 파일은 받지 않습니다.")
    lowered = head.lstrip().lower()
    if mime.split(";")[0].lower() in {"text/html", "application/xhtml+xml"} or lowered.startswith((b"<!doctype html", b"<html", b"<head", b"<script")):
        raise ValueError("파일 대신 웹페이지가 도착했습니다. 로그인 없이 받을 수 있는 파일 주소를 사용하세요.")
    suffix = ""
    for prefix, ext in ((b"\x89PNG\r\n\x1a\n", ".png"), (b"\xff\xd8\xff", ".jpg"), (b"GIF8", ".gif"),
                        (b"%PDF-", ".pdf"), (b"II*\0", ".tiff"), (b"MM\0*", ".tiff"), (b"BM", ".bmp"),
                        (b"fLaC", ".flac"), (b"ID3", ".mp3"), (b"OggS", ".ogg")):
        if head.startswith(prefix):
            suffix = ext
            break
    for prefix, ext in ((b"wOFF", ".woff"), (b"wOF2", ".woff2"), (b"OTTO", ".otf"), (b"\x00\x01\x00\x00", ".ttf")):
        if head.startswith(prefix):
            suffix = ext
    if head.startswith(b"RIFF"):
        suffix = {b"WEBP": ".webp", b"WAVE": ".wav", b"AVI ": ".avi"}.get(head[8:12], "")
    if len(head) > 12 and head[4:8] == b"ftyp":
        suffix = ".avif" if head[8:12] in {b"avif", b"avis"} else ".heic" if head[8:12] in {b"heic", b"heix", b"mif1"} else ".m4a" if head[8:12] == b"M4A " else ".mp4"
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            suffix = next((ext for marker, ext in (("word/document.xml", ".docx"), ("ppt/presentation.xml", ".pptx"),
                          ("xl/workbook.xml", ".xlsx"), ("Contents/section0.xml", ".hwpx"), ("META-INF/container.xml", ".epub")) if marker in names), ".zip")
            if suffix == ".zip" and Path(name).suffix.lower() == ".cbz" and any(Path(n).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"} for n in names):
                suffix = ".cbz"
            if suffix == ".zip" and Path(name).suffix.lower() == ".kmz" and any(n.lower().endswith(".kml") for n in names):
                suffix = ".kmz"
    if not suffix:
        detected = FORMATS.detect(name)
        if not detected or not head:
            raise ValueError("웹 파일의 실제 형식을 식별하지 못했습니다.")
        if detected.id not in {"txt", "md", "csv", "tsv", "srt", "vtt", "svg", "hwp", "mp3", "webm", "mkv", "tar", "gz", "tar.gz", "rar", "wma", "wmv", "aiff",
                               "json", "jsonl", "yaml", "toml", "xml", "fb2", "eml", "mhtml", "obj", "stl", "ply", "geojson", "kml", "gpx"}:
            raise ValueError("파일 이름과 실제 파일 헤더가 일치하지 않습니다.")
        suffix = detected.suffix
    return safe_name(Path(name).stem) + suffix


class Redirects(urllib.request.HTTPRedirectHandler):
    max_redirections = 5
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            parsed = validate_url(newurl)
            if req.full_url.startswith("https:") and parsed.scheme != "https":
                raise ValueError("HTTPS에서 HTTP로 내려가는 리디렉션은 받지 않습니다.")
        except ValueError:
            fp.close()
            raise
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch(url, folder, suggested="download", *, max_bytes=MAX_BYTES, timeout=15):
    validate_url(url)
    folder.mkdir(parents=True, exist_ok=True)
    temporary = folder / "transfer.partial"
    try:
        request = urllib.request.Request(url, headers={"User-Agent": "MagicConverter/6", "Accept-Encoding": "identity"})
        opener = urllib.request.build_opener(Redirects())
        with opener.open(request, timeout=timeout) as response:
            if response.status != 200:
                raise ValueError(f"다운로드 HTTP {response.status}")
            length = response.headers.get("Content-Length")
            expected = int(length) if length is not None else None
            if expected is not None and (expected < 0 or expected > max_bytes):
                raise ValueError("다운로드 크기 제한을 초과했습니다.")
            name = response.headers.get_filename() or suggested
            name = safe_name(name)
            mime = response.headers.get("Content-Type", "")
            count, last = 0, 0.0
            with temporary.open("xb") as stream:
                while chunk := response.read(65536):
                    count += len(chunk)
                    if count > max_bytes:
                        raise ValueError("다운로드 크기 제한을 초과했습니다.")
                    stream.write(chunk)
                    if time.monotonic() - last > .1:
                        print(json.dumps({"bytes": count, "total": expected}), flush=True)
                        last = time.monotonic()
                stream.flush()
                os.fsync(stream.fileno())
            if expected is not None and count != expected:
                raise ValueError("다운로드가 완료되기 전에 연결이 끊어졌습니다.")
            actual = identify(temporary, name, mime)
            target = folder / actual
            if target.exists():
                raise ValueError("다운로드 임시 파일 이름이 충돌했습니다.")
            temporary.rename(target)
            return target
    except urllib.error.HTTPError as exc:
        code = exc.code
        exc.close()
        raise ValueError(f"다운로드 HTTP {code}") from None
    finally:
        temporary.unlink(missing_ok=True)


def helper_main(request_path, report_path):
    from magic_converter.core.settings.json_store import read_json, write_json
    try:
        request = read_json(Path(request_path))
        path = fetch(request["url"], Path(request["folder"]), request["name"],
                     max_bytes=request.get("max_bytes", MAX_BYTES), timeout=request.get("timeout", 15))
        write_json(Path(report_path), {"ok": True, "path": str(path)})
        return 0
    except Exception as exc:
        write_json(Path(report_path), {"ok": False, "error": str(exc)[:1000]})
        return 1


def materialize(item, folder, progress):
    from magic_converter.core.conversion.converter import ConversionError
    from magic_converter.core.jobs.job_control import check_cancelled, report_stage
    from magic_converter.core.jobs.process_runner import run_process
    from magic_converter.core.settings.json_store import read_json, write_json
    folder.mkdir(parents=True, exist_ok=True)
    report_stage("웹 파일 다운로드 중")
    if item.data:
        path = folder / "transfer.partial"
        path.write_bytes(item.data)
        actual = folder / identify(path, item.name)
        path.rename(actual)
        check_cancelled()
        return actual
    request, report = folder.parent / "fetch-request.json", folder.parent / "fetch-report.json"
    write_json(request, {"url": item.url, "folder": str(folder), "name": item.name})
    command = ([sys.executable, "--web-fetch"] if getattr(sys, "frozen", False)
               else [sys.executable, "-m", "magic_converter.core.automation.web_transfer"])
    def output(line):
        try:
            data = json.loads(line)
            progress(min(20, int(data["bytes"] * 20 / data["total"])) if data.get("total") else None)
        except (ValueError, TypeError, KeyError):
            pass
    outcome = run_process([*command, str(request), str(report)], timeout=600, idle_timeout=30, on_output=output)
    data = read_json(report) if report.is_file() else {}
    if outcome.returncode or not data.get("ok"):
        raise ConversionError(data.get("error", "웹 파일 다운로드가 실패했습니다."))
    path = Path(data["path"]).resolve()
    if not path.is_relative_to(folder.resolve()) or not path.is_file():
        raise ConversionError("웹 파일의 다운로드 경로가 올바르지 않습니다.")
    return path


if __name__ == "__main__":
    raise SystemExit(helper_main(*sys.argv[1:]))
