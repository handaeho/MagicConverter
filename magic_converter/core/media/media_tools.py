"""Bounded FFmpeg tools with structural probing and cancellation supervision."""

import json
import math
from pathlib import Path

from PIL import Image

from magic_converter.core.conversion.converter import ConversionError, FFmpegProgressParser, find_ffprobe, media_command
from magic_converter.core.conversion.formats import FORMATS
from magic_converter.core.jobs.job_control import check_cancelled, checked
from magic_converter.core.jobs.process_runner import run_process


def probe_media(source: Path, ffmpeg: str, *, chapters: bool = False) -> dict:
    probe = find_ffprobe(ffmpeg)
    if not probe:
        raise ConversionError("미디어 도구에는 FFprobe가 필요합니다.")
    args = [probe, "-v", "error", "-show_streams", "-show_format"]
    if chapters:
        args.append("-show_chapters")
    result = run_process([*args, "-of", "json", str(source)], timeout=30)
    if result.returncode:
        raise ConversionError(result.stderr[-1200:] or "미디어 정보를 읽지 못했습니다.")
    data = json.loads(result.stdout)
    if not data.get("streams"):
        raise ConversionError("미디어 스트림이 없습니다.")
    return data


def _duration(info):
    try:
        duration = float(info.get("format", {}).get("duration", 0))
        return duration if math.isfinite(duration) and duration > 0 else None
    except (TypeError, ValueError):
        return None


def _check_range(info, options):
    duration = _duration(info)
    if duration is not None and options.start_ms / 1000 >= duration:
        raise ConversionError("시작 시간이 미디어 길이를 벗어났습니다.")
    remaining = None if duration is None else duration - options.start_ms / 1000
    length = options.duration_ms / 1000
    return min(length, remaining) if length and remaining is not None else length or remaining


def _run(command, duration, progress):
    parser = FFmpegProgressParser(duration)
    def update(line):
        value = parser.feed(line)
        if value is not None:
            progress(value)
    result = run_process(command, idle_timeout=600, on_output=update)
    if result.returncode:
        raise ConversionError(result.stderr[-2400:].strip() or f"FFmpeg 종료 코드: {result.returncode}")


def _track_signature(stream):
    tags = {key.lower(): value for key, value in stream.get("tags", {}).items()}
    language = tags.get("language")
    return (stream.get("codec_type"), stream.get("codec_name"),
            None if language in {None, "und"} else language,
            tuple(tags.get(key) for key in ("title", "filename", "mimetype")),
            frozenset(key for key, enabled in stream.get("disposition", {}).items() if enabled))


def _check_remux(source, output, notice):
    if [_track_signature(item) for item in source["streams"]] != [_track_signature(item) for item in output["streams"]]:
        raise ConversionError("MKV에 모든 트랙의 코덱·이름·언어·기본 설정을 유지할 수 없습니다.")
    before, after = source.get("chapters", []), output.get("chapters", [])
    if len(before) != len(after):
        raise ConversionError("MKV에 모든 챕터를 유지할 수 없습니다.")
    for left, right in zip(before, after):
        if (left.get("tags", {}).get("title") != right.get("tags", {}).get("title")
                or any(abs(float(left[field]) - float(right[field])) > 0.001
                       for field in ("start_time", "end_time"))):
            raise ConversionError("MKV에 챕터 이름 또는 시간을 유지할 수 없습니다.")
    if any(left.get("time_base") != right.get("time_base")
           for left, right in zip(source["streams"], output["streams"])):
        notice("트랙을 재인코딩하지 않았습니다. MKV의 시간 단위에 맞춰 일부 재생 시각이 반올림될 수 있습니다.")


def _remux_mkv(source, destination, ffmpeg, progress, notice):
    info = probe_media(source, ffmpeg, chapters=True)
    command = [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y",
        "-nostats", "-stats_period", "0.2", "-progress", "pipe:1",
        "-copyts", "-i", str(source), "-map", "0", "-map_metadata", "0", "-map_chapters", "0",
        "-c", "copy", "-default_mode", "passthrough", "-avoid_negative_ts", "disabled", str(destination),
    ]
    _run(command, _duration(info), progress)
    check_cancelled()
    _check_remux(info, probe_media(destination, ffmpeg, chapters=True), notice)
    progress(100)


def convert_media_tool(source: Path, destination: Path, key: str, options, ffmpeg, progress, notice=lambda _: None):
    if not ffmpeg:
        raise ConversionError("FFmpeg가 필요합니다.")
    if key == "tool:media_remux_mkv":
        _remux_mkv(source, destination, ffmpeg, progress, notice)
        return
    info = probe_media(source, ffmpeg)
    length = _check_range(info, options)
    format_id = FORMATS.detect(source).id
    action = "media:mp4" if key == "tool:media_mute" else (
        f"media:{format_id}" if format_id in FORMATS.ids("video") or format_id == "mp3" else f"audio:{format_id}")
    command = media_command(ffmpeg, source, destination, action)
    extra = []
    if key == "tool:media_trim":
        extra += ["-ss", f"{options.start_ms / 1000:.3f}"]
        if options.duration_ms:
            extra += ["-t", f"{options.duration_ms / 1000:.3f}"]
    else:
        extra += ["-an"]
    command[-1:-1] = extra
    _run(command, length, progress)
    result = probe_media(destination, ffmpeg)
    types = {stream.get("codec_type") for stream in result["streams"]}
    if key == "tool:media_mute" and ("video" not in types or "audio" in types):
        raise ConversionError("음소거 출력의 영상·음성 구조가 올바르지 않습니다.")
    check_cancelled()


def extract_media_frames(source: Path, folder: Path, options, ffmpeg, progress):
    if not ffmpeg:
        raise ConversionError("FFmpeg가 필요합니다.")
    info = probe_media(source, ffmpeg)
    if not any(stream.get("codec_type") == "video" for stream in info["streams"]):
        raise ConversionError("프레임을 추출할 영상 스트림이 없습니다.")
    length = _check_range(info, options)
    command = [ffmpeg, "-hide_banner", "-loglevel", "error", "-nostdin", "-y", "-progress", "pipe:1", "-nostats",
               "-ss", f"{options.start_ms / 1000:.3f}", "-i", str(source)]
    if options.duration_ms:
        command += ["-t", f"{options.duration_ms / 1000:.3f}"]
    command += ["-map", "0:v:0", "-an", "-sn", "-dn", "-vf", f"select=not(mod(n\\,{options.frame_interval}))",
                "-fps_mode", "vfr", "-frames:v", str(options.max_frames + 1), "-threads", "2", str(folder / "frame_%06d.png")]
    _run(command, length, progress)
    paths = sorted(folder.glob("frame_*.png"))
    if not paths:
        raise ConversionError("선택 구간에 추출할 프레임이 없습니다.")
    if len(paths) > options.max_frames:
        raise ConversionError("최대 프레임 수를 초과했습니다. 구간을 줄이거나 프레임 간격을 늘리세요.")
    dimensions = None
    for path in checked(paths):
        with Image.open(path) as image:
            image.load()
            if dimensions is not None and dimensions != image.size:
                raise ConversionError("추출된 프레임의 크기가 일정하지 않습니다.")
            dimensions = image.size
    manifest = {"source": source.name, "start_ms": options.start_ms, "duration_ms": options.duration_ms,
                "frame_interval": options.frame_interval, "width": dimensions[0], "height": dimensions[1],
                "frames": [path.name for path in paths]}
    (folder / "frames.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
