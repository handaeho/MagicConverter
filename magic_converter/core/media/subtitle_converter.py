"""SRT/WebVTT conversion without dropping multi-line cue text."""

from __future__ import annotations

import html
import re
from pathlib import Path

from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.media.subtitle_tools import TIME_PATTERN, format_time, parse_time, read_subtitle
from magic_converter.core.jobs.job_control import checked, check_cancelled

TIMESTAMP = re.compile(r"(?P<start>(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3})\s*-->\s*(?P<end>(?:\d{2,}:)?\d{2}:\d{2}[.,]\d{3})(?:\s+.*)?$")


def convert_subtitle(source: Path, destination: Path, suffix: str, *, offset_ms: int = 0, notice=lambda _: None) -> None:
    cues, headers = read_subtitle(source)
    if suffix not in {".srt", ".vtt", ".txt"}:
        raise ConversionError("지원하지 않는 자막 출력입니다.")
    blocks, dropped = [], 0
    for cue in checked(cues):
        start, end = max(0, cue.start_ms + offset_ms), cue.end_ms + offset_ms
        if end <= 0:
            dropped += 1
            continue
        content = cue.text
        if offset_ms:
            content = re.sub(rf"<({TIME_PATTERN})>", lambda match: "<" + format_time(max(0, parse_time(match[1]) + offset_ms)) + ">", content)
        if suffix == ".txt":
            blocks.append(html.unescape(re.sub(r"<[^>]+>", "", content)))
        elif suffix == ".srt":
            blocks.append(f"{len(blocks) + 1}\n{format_time(start, ',')} --> {format_time(end, ',')}\n{content}")
        else:
            identifier = cue.identifier + "\n" if cue.identifier else ""
            settings = " " + cue.settings if cue.settings else ""
            blocks.append(f"{identifier}{format_time(start)} --> {format_time(end)}{settings}\n{content}")
    if not blocks:
        raise ConversionError("시간 이동 후 남은 자막 구간이 없습니다.")
    if dropped:
        notice(f"시간 이동 후 0초 이전에 끝나는 자막 {dropped}개를 제외했습니다.")
    if suffix == ".srt" and (any(cue.settings for cue in cues) or any(header.startswith(("STYLE", "REGION")) for header in headers)):
        notice("SRT가 지원하지 않는 WebVTT 위치·스타일·영역 설정은 제외했습니다.")
    if suffix == ".vtt":
        header = headers if headers and headers[0].startswith("WEBVTT") else ["WEBVTT", *headers]
        blocks = [*header, *blocks]
    check_cancelled()
    destination.write_text("\n\n".join(blocks) + "\n", encoding="utf-8")
