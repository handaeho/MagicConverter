"""Versioned, immutable recipes and wheel profiles; no UI or encoder imports."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
import re
import string

SCHEMA_VERSION = 3
MAX_SLOTS = 12


class SettingsError(ValueError):
    pass


def _integer(value, name, low, high):
    if type(value) is not int or not low <= value <= high:
        raise SettingsError(f"{name}: {low}~{high} 범위의 정수가 필요합니다.")
    return value


def _text(value, name, limit=500):
    if not isinstance(value, str) or len(value) > limit or any(ord(c) < 32 for c in value):
        raise SettingsError(f"{name}: 올바른 문자열이 필요합니다.")
    return value


@dataclass(frozen=True)
class EncoderOptions:
    image_quality: int = 90
    image_max_edge: int = 0
    video_crf: int | None = None
    audio_bitrate: int | None = None

    def __post_init__(self):
        _integer(self.image_quality, "이미지 품질", 1, 100)
        _integer(self.image_max_edge, "긴 변", 0, 16384)
        if self.video_crf is not None:
            _integer(self.video_crf, "영상 CRF", 0, 51)
        if self.audio_bitrate is not None:
            _integer(self.audio_bitrate, "오디오 비트레이트", 32, 512)


@dataclass(frozen=True)
class ToolOptions:
    pdf_pages: str = ""
    rotation: int = 90
    flip: str = "horizontal"
    crop_x: int = 0
    crop_y: int = 0
    crop_width: int = 0
    crop_height: int = 0
    start_ms: int = 0
    duration_ms: int = 0
    frame_interval: int = 1
    max_frames: int = 10000
    subtitle_offset_ms: int = 1000

    def __post_init__(self):
        _text(self.pdf_pages, "PDF 페이지", 1000)
        if self.pdf_pages and not re.fullmatch(r"\s*[1-9]\d*(?:\s*-\s*[1-9]\d*)?(?:\s*,\s*[1-9]\d*(?:\s*-\s*[1-9]\d*)?)*\s*", self.pdf_pages):
            raise SettingsError("PDF 페이지는 1-3,5처럼 지정하세요.")
        if type(self.rotation) is not int or self.rotation not in {0, 90, 180, 270}:
            raise SettingsError("회전은 0·90·180·270도 중 하나를 지정하세요.")
        if self.flip not in {"horizontal", "vertical"}:
            raise SettingsError("반전 방향이 올바르지 않습니다.")
        for name in ("crop_x", "crop_y", "crop_width", "crop_height"):
            _integer(getattr(self, name), "자르기 좌표·크기", 0, 100000)
        for name in ("start_ms", "duration_ms"):
            _integer(getattr(self, name), "미디어 시간(ms)", 0, 604800000)
        _integer(self.frame_interval, "프레임 간격", 1, 10000)
        _integer(self.max_frames, "최대 프레임", 1, 100000)
        _integer(self.subtitle_offset_ms, "자막 이동(ms)", -604800000, 604800000)

    def for_action(self, action_key):
        from magic_converter.core.actions.tool_definitions import TOOL_BY_KEY
        fields = TOOL_BY_KEY[action_key].fields if action_key in TOOL_BY_KEY else ()
        return ToolOptions(**{name: getattr(self, name) for name in fields})


@dataclass(frozen=True)
class OutputRule:
    location: str = "source"
    directory: str = ""
    template: str = "{stem}_{tag}"
    collision: str = "rename"

    def __post_init__(self):
        if self.location not in {"source", "folder"} or self.collision not in {"rename", "error"}:
            raise SettingsError("알 수 없는 출력 위치 또는 이름 충돌 정책입니다.")
        _text(self.directory, "출력 폴더", 2000)
        if self.location == "folder" and (not self.directory or not Path(self.directory).is_absolute()):
            raise SettingsError("출력 폴더는 절대 경로로 지정하세요.")
        _text(self.template, "파일 이름", 160)
        try:
            for _, field, spec, conversion in string.Formatter().parse(self.template):
                if field is not None and (field not in {"stem", "tag", "preset"} or spec or conversion):
                    raise ValueError()
            sample = self.template.format(stem="sample", tag="converted", preset="recipe")
        except (ValueError, KeyError, IndexError):
            raise SettingsError("이름에는 {stem}, {tag}, {preset}만 사용할 수 있습니다.") from None
        self._valid_name(sample)

    @staticmethod
    def _valid_name(name):
        if (not name or name in {".", ".."} or re.search(r'[<>:"/\\|?*\x00-\x1f]', name)
                or name[-1:] in {".", " "} or len(name) > 200
                or re.fullmatch(r"(?i:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", name)):
            raise SettingsError("출력 이름은 Windows 파일 이름으로 사용할 수 있어야 합니다.")

    def destination(self, source: Path, tag: str, preset_name: str = "") -> tuple[Path, str]:
        safe_preset = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", preset_name).strip(" .") or "converted"
        name = self.template.format(stem=source.stem, tag=tag, preset=safe_preset)
        self._valid_name(name)
        return (source.parent if self.location == "source" else Path(self.directory)), name


@dataclass(frozen=True)
class Preset:
    id: str
    name: str
    action_key: str
    options: EncoderOptions = EncoderOptions()
    output: OutputRule | None = None
    tools: ToolOptions = ToolOptions()

    def __post_init__(self):
        from magic_converter.core.conversion.file_router import ALL_ACTIONS
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.id):
            raise SettingsError("프리셋 ID가 올바르지 않습니다.")
        if not _text(self.name, "프리셋 이름", 60).strip() or self.action_key not in ALL_ACTIONS:
            raise SettingsError("프리셋 이름과 지원 작업을 지정하세요.")
        if self.tools != self.tools.for_action(self.action_key):
            raise SettingsError("이 작업에서 지원하지 않는 도구 옵션이 지정되어 있습니다.")
        image = self.action_key.startswith("image:")
        if not image and (self.options.image_quality != 90 or self.options.image_max_edge):
            raise SettingsError("이미지 품질·크기는 이미지 변환 프리셋에만 적용할 수 있습니다.")
        if self.options.video_crf is not None and self.action_key not in {"media:mp4", "media:mov", "media:mkv", "media:webm"}:
            raise SettingsError("CRF는 MP4·MOV·MKV·WebM 프리셋에 적용할 수 있습니다.")
        if self.options.audio_bitrate is not None and self.action_key not in {
                "media:mp4", "media:mov", "media:mkv", "media:webm", "media:avi", "media:wmv", "media:mp3",
                "audio:m4a", "audio:ogg", "audio:opus", "audio:wma"}:
            raise SettingsError("비트레이트를 지정할 수 없는 작업입니다.")


@dataclass(frozen=True)
class WheelProfile:
    id: str
    name: str
    mode: str
    preset_ids: tuple[str, ...]

    def __post_init__(self):
        if not re.fullmatch(r"[a-zA-Z0-9_-]{1,64}", self.id) or not _text(self.name, "프로필 이름", 60).strip():
            raise SettingsError("휠 프로필 이름과 ID가 올바르지 않습니다.")
        if self.mode not in {"convert", "action"} or not 1 <= len(self.preset_ids) <= MAX_SLOTS:
            raise SettingsError(f"휠 프로필은 1~{MAX_SLOTS}개 슬롯을 지정하세요.")
        if len(set(self.preset_ids)) != len(self.preset_ids):
            raise SettingsError("프로필에 같은 프리셋이 중복되어 있습니다.")


@dataclass(frozen=True)
class Preferences:
    enabled: bool = True
    interval_ms: int = 35
    threshold: int = 15
    ffmpeg_path: str = ""
    pass
    presets: tuple[Preset, ...] = ()
    profiles: tuple[WheelProfile, ...] = ()
    convert_profile: str = ""
    action_profile: str = ""
    output: OutputRule = OutputRule()
    history_limit: int = 200
    release_modifier_inside: bool = False

    def __post_init__(self):
        pass
        _integer(self.interval_ms, "감지 주기", 30, 50)
        _integer(self.threshold, "드래그 거리", 5, 80)
        _integer(self.history_limit, "기록 보관 수", 0, 1000)
        _text(self.ffmpeg_path, "FFmpeg 경로", 2000)
        if len(self.presets) > 200 or len(self.profiles) > 50:
            raise SettingsError("프리셋·프로필 개수가 제한을 초과했습니다.")
        ids = {p.id for p in self.presets}
        if len(ids) != len(self.presets) or len({p.id for p in self.profiles}) != len(self.profiles):
            raise SettingsError("프리셋·프로필 ID는 중복될 수 없습니다.")
        for profile in self.profiles:
            if not set(profile.preset_ids) <= ids:
                raise SettingsError(f"{profile.name}: 삭제되거나 없는 프리셋을 참조합니다.")
        for mode in ("convert", "action"):
            selected = getattr(self, mode + "_profile")
            if selected and not any(p.id == selected and p.mode == mode for p in self.profiles):
                raise SettingsError("선택한 휠 프로필을 찾을 수 없습니다.")

    def base_values(self):
        pass

    def to_dict(self):
        return {"version": SCHEMA_VERSION, **asdict(self)}

    @classmethod
    def from_dict(cls, data):
        if not isinstance(data, dict) or type(data.get("version")) is not int or data["version"] not in {1, 2, SCHEMA_VERSION}:
            raise SettingsError("지원하지 않는 설정 버전입니다. 이 파일을 덮어쓰지 않습니다.")
        try:
            values = {key: value for key, value in data.items() if key in cls.__dataclass_fields__}
            values["output"] = OutputRule(**data.get("output", {}))
            values["presets"] = tuple(Preset(p["id"], p["name"], p["action_key"], EncoderOptions(**p.get("options", {})),
                                             OutputRule(**p["output"]) if p.get("output") is not None else None,
                                             ToolOptions(**p.get("tools", {})))
                                      for p in data.get("presets", []))
            values["profiles"] = tuple(WheelProfile(p["id"], p["name"], p["mode"], tuple(p["preset_ids"]))
                                       for p in data.get("profiles", []))
            return cls(**values)
        except (TypeError, KeyError, AttributeError) as exc:
            raise SettingsError("설정 파일의 구조가 올바르지 않습니다.") from exc


def default_preferences(**base) -> Preferences:
    presets = tuple(Preset(id, name, key) for id, name, key in (
        ("jpg", "JPG", "image:jpg"), ("png", "PNG", "image:png"), ("webp", "WebP", "image:webp"),
        ("pdf", "PDF", "document:pdf"), ("docx", "DOCX", "document:docx"), ("txt", "TXT", "document:txt"),
        ("pages", "페이지 PNG", "document:png"), ("mp4", "MP4", "media:mp4"), ("mp3", "MP3", "media:mp3"),
        ("zip", "ZIP 압축", "zip"), ("strip", "메타데이터 제거", "strip"), ("copy", "경로 복사", "copy_path")))
    profiles = (WheelProfile("images", "이미지", "convert", ("jpg", "png", "webp", "pdf")),
                WheelProfile("documents", "문서", "convert", ("pdf", "docx", "txt", "pages")),
                WheelProfile("actions", "빠른 액션", "action", ("copy", "strip", "zip")))
    return Preferences(presets=presets, profiles=profiles, **base)
