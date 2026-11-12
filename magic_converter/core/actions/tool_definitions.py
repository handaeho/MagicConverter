"""Tool identities shared by routing, presets and the options editor."""

from dataclasses import dataclass
from magic_converter.core.conversion.formats import FORMATS


@dataclass(frozen=True)
class Tool:
    key: str
    label: str
    description: str
    sources: frozenset[str]
    target: str | None
    engine: str
    fields: tuple[str, ...] = ()
    folder: bool = False
    combined: bool = False

    @property
    def wheel_label(self):
        if self.key in {"tool:pdf_select", "tool:pdf_split"}:
            return "페이지\n" + self.label.split()[-1]
        if self.key == "tool:pdf_compress":
            return "무손실\n압축"
        if self.key in {"tool:image_frames", "tool:media_frames"}:
            return ("이미지" if self.key == "tool:image_frames" else "영상") + "\n프레임"
        return self.label.replace(" ", "\n", 1)


_images = FORMATS.ids("image") | {"gif"}
_videos = FORMATS.ids("video")
_media = _videos | FORMATS.ids("audio")
_pdf = frozenset({"pdf"})
_regular_files = frozenset(item.id for item in FORMATS.known if item.readable and item.id != "rtfd")
TOOLS = tuple(Tool("tool:" + key, label, description, frozenset(sources), target, engine, fields, folder, combined)
    for key, label, description, sources, target, engine, fields, folder, combined in (
    ("copy_original", "원본 그대로 복사", "내용을 다시 저장하지 않고 원본 파일을 그대로 복사", _regular_files, None, "copy", (), False, False),
    ("pdf_merge", "PDF 병합", "선택한 PDF·이미지를 입력 순서대로 하나의 PDF로 병합", _pdf | _images, "pdf", "pdf_tools", (), False, True),
    ("pdf_select", "PDF 페이지 추출", "페이지 범위·순서 지정 (예: 3,1-2) · 빈 값은 전체", _pdf, "pdf", "pdf_tools", ("pdf_pages",), False, False),
    ("pdf_split", "PDF 페이지 분할", "선택한 페이지를 한 장씩 PDF로 저장", _pdf, "pdf", "pdf_tools", ("pdf_pages",), True, False),
    ("pdf_rotate", "PDF 회전", "선택 페이지를 시계 방향으로 회전", _pdf, "pdf", "pdf_tools", ("pdf_pages", "rotation"), False, False),
    ("pdf_compress", "PDF 무손실 압축", "내용 스트림·중복 객체 정리 · 이미 최적화된 파일은 원본 크기 유지", _pdf, "pdf", "pdf_tools", (), False, False),
    ("image_rotate", "이미지 회전", "모든 프레임을 시계 방향으로 회전해 PNG/APNG로 저장", _images, "png", "image_tools", ("rotation",), False, False),
    ("image_flip", "이미지 반전", "모든 프레임을 좌우/상하 반전해 PNG/APNG로 저장", _images, "png", "image_tools", ("flip",), False, False),
    ("image_grayscale", "흑백 이미지", "투명도를 유지한 흑백 PNG/APNG 생성", _images, "png", "image_tools", (), False, False),
    ("image_crop", "이미지 자르기", "픽셀 좌표로 모든 프레임 자르기 · 너비/높이 0은 남은 영역", _images, "png", "image_tools", ("crop_x", "crop_y", "crop_width", "crop_height"), False, False),
    ("image_frames", "이미지 프레임 추출", "GIF/APNG/TIFF 등 모든 프레임을 PNG와 시간 정보로 저장", _images, "png", "image_tools", ("frame_interval", "max_frames"), True, False),
    ("media_remux_mkv", "MKV 트랙 복사", "호환되는 영상·음성·자막·첨부를 재인코딩 없이 MKV에 담기", _videos - {"gif"}, "mkv", "ffmpeg", (), False, False),
    ("media_trim", "미디어 구간 추출", "시작·길이를 지정해 같은 형식으로 재인코딩 · 길이 0은 끝까지", _media, None, "ffmpeg", ("start_ms", "duration_ms"), False, False),
    ("media_mute", "영상 음소거", "음성 트랙을 제거한 H.264 MP4 생성", _videos - {"gif"}, "mp4", "ffmpeg", (), False, False),
    ("media_frames", "영상 프레임 추출", "원본 프레임 간격으로 PNG 추출 · 시작·길이·간격·최대 수 설정", _videos, "png", "ffmpeg", ("start_ms", "duration_ms", "frame_interval", "max_frames"), True, False),
    ("subtitle_shift", "자막 시간 이동", "SRT/VTT 시간 이동 · 음수 시작은 0, 완전히 음수인 구간은 제외", FORMATS.ids("subtitle"), None, "subtitle_tools", ("subtitle_offset_ms",), False, False),
    ("archive_extract", "압축 해제", "ZIP·TAR 계열·GZIP을 새 폴더에 해제 · 경로·링크·중복·크기 검사", FORMATS.ids("archive") - {"rar"}, None, "archive_tools", (), True, False),
))
TOOL_BY_KEY = {tool.key: tool for tool in TOOLS}
COMBINED_ACTIONS = frozenset({"zip", "terminal", *(tool.key for tool in TOOLS if tool.combined)})

TOOL_FIELD_LABELS = {"pdf_pages": "PDF 페이지 / 순서", "rotation": "시계 방향 회전", "flip": "반전 방향",
    "crop_x": "자르기 X", "crop_y": "자르기 Y", "crop_width": "너비 (0: 나머지)", "crop_height": "높이 (0: 나머지)",
    "start_ms": "시작", "duration_ms": "길이 (0: 끝까지)", "frame_interval": "프레임 간격", "max_frames": "최대 프레임",
    "subtitle_offset_ms": "자막 시간 이동"}


def tool_summary(key, options):
    values = []
    for field in TOOL_BY_KEY[key].fields:
        value = getattr(options, field)
        if field == "flip":
            value = "좌우" if value == "horizontal" else "상하"
        elif field.endswith("_ms"):
            value = f"{value} ms"
        elif field == "rotation":
            value = f"{value}°"
        elif value == "":
            value = "전체"
        values.append(f"{TOOL_FIELD_LABELS[field]}: {value}")
    return " · ".join(values) or TOOL_BY_KEY[key].description
