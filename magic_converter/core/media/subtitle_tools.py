"""Validated SRT/WebVTT cues, preserving cue IDs, settings and multiline text."""

from dataclasses import dataclass
import re
from pathlib import Path

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.documents.document_converter import decode_text
from magic_converter.core.jobs.job_control import checked

TIME_PATTERN = r"(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3}"
TIMESTAMP = re.compile(rf"(?P<start>{TIME_PATTERN})\s*-->\s*(?P<end>{TIME_PATTERN})(?P<settings>\s+.*)?$")


@dataclass(frozen=True)
class Cue:
    start_ms: int
    end_ms: int
    text: str
    identifier: str = ""
    settings: str = ""


def parse_time(text):
    parts = text.replace(",", ".").split(":")
    if len(parts) == 2:
        parts.insert(0, "0")
    hours, minutes, seconds = int(parts[0]), int(parts[1]), parts[2].split(".")
    seconds, milliseconds = int(seconds[0]), int(seconds[1])
    if minutes >= 60 or seconds >= 60:
        raise ConversionError("자막의 분·초 값은 60보다 작아야 합니다.")
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + milliseconds


def format_time(milliseconds, separator="."):
    seconds, millis = divmod(milliseconds, 1000)
    minutes, seconds = divmod(seconds, 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}{separator}{millis:03d}"


def read_subtitle(source: Path):
    if source.stat().st_size > 64 * 1024 * 1024:
        raise ConversionError("자막 파일이 64 MiB 제한을 초과했습니다.")
    text = decode_text(source.read_bytes()).replace("\r\n", "\n").replace("\r", "\n").lstrip("\ufeff")
    cues, headers = [], []
    for block in checked(re.split(r"\n\s*\n", text)):
        lines = block.strip().splitlines()
        if not lines:
            continue
        if lines[0].startswith("WEBVTT"):
            headers.append("\n".join(lines))
            continue
        if lines[0] in {"STYLE", "REGION", "NOTE"} or lines[0].startswith("NOTE "):
            headers.append("\n".join(lines))
            continue
        found = False
        for index, line in enumerate(lines[:2]):
            match = TIMESTAMP.fullmatch(line.strip())
            if match is None:
                continue
            start, end = parse_time(match["start"]), parse_time(match["end"])
            content = "\n".join(lines[index + 1:])
            if end <= start or not content:
                raise ConversionError("자막 구간은 종료가 시작보다 늦고 본문이 있어야 합니다.")
            cues.append(Cue(start, end, content, lines[0] if index else "", (match["settings"] or "").strip()))
            found = True
            break
        if not found:
            raise ConversionError("자막 구간의 시간 형식이 올바르지 않습니다.")
        if len(cues) > 100000:
            raise ConversionError("자막 구간이 100,000개 제한을 초과했습니다.")
    if not cues:
        raise ConversionError("유효한 SRT/VTT 자막 구간을 찾지 못했습니다.")
    return cues, headers
