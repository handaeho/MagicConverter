"""Pure routing rules, shared by the overlay and the worker.

Extensions determine eligibility; an encoder validates the actual file contents.
For a batch, an action must support every input. No partial, silent filtering.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Literal, TYPE_CHECKING

if TYPE_CHECKING:
    from magic_converter.core.settings.preferences import Preset

from magic_converter.core.conversion.formats import (ARCHIVE_EXTENSIONS, AUDIO_EXTENSIONS, DOCUMENT_EXTENSIONS,
                      HWP_EXTENSIONS, IMAGE_EXTENSIONS, MEDIA_EXTENSIONS,
                      SHEET_EXTENSIONS, SLIDE_EXTENSIONS, VIDEO_EXTENSIONS)
from magic_converter.core.actions.tool_definitions import TOOLS

Mode = Literal["convert", "action"]


@dataclass(frozen=True)
class Action:
    key: str
    label: str
    description: str
    suffix: str = ""


ACTION_ACTIONS = (
    Action("copy_path", "경로 복사", "전체 경로를 줄바꿈으로 구분해 복사"),
    Action("strip", "메타데이터 제거", "지원 이미지·미디어의 메타데이터를 제거한 복사본"),
    Action("zip", "ZIP 압축", "선택 파일을 하나의 ZIP으로 묶기", ".zip"),
    Action("terminal", "터미널", "각 파일의 폴더에서 PowerShell 열기"),
)
TOOL_ACTIONS = tuple(Action(tool.key, tool.label, tool.description) for tool in TOOLS)
ALL_ACTIONS = {action.key: action for action in (*CONVERT_ACTIONS, *ACTION_ACTIONS, *TOOL_ACTIONS)}


@dataclass(frozen=True)
class Slot:
    action: Action
    enabled: bool
    reason: str = ""
    preset: Preset | None = None


def normalize_paths(paths: Iterable[str | Path]) -> tuple[Path, ...]:
    """Deduplicate paths in Explorer order without opening or reading files."""
    result: list[Path] = []
    seen: set[str] = set()
    for item in paths:
        path = Path(item).absolute()
        identity = str(path).casefold()
        if identity not in seen:
            seen.add(identity)
            result.append(path)
    return tuple(result)


def supports(action: Action, path: Path) -> bool:
    from magic_converter.core.conversion.builtin_converters import create_catalog
    return create_catalog().assess(path, action.key).route is not None




def describe_files(paths: Iterable[str | Path]) -> str:
    files = normalize_paths(paths)
    if not files:
        return "파일 대기 중"
    extensions = sorted({path.suffix[1:].upper() or "파일" for path in files})
    kinds = "/".join(extensions[:3]) + ("…" if len(extensions) > 3 else "")
    return f"{len(files)}개 · {kinds}"

