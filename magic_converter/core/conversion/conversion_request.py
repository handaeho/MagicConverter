"""Validated, per-file conversion plans shared by every application entrypoint."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Iterable

from magic_converter.core.conversion.conversion_catalog import Converter, ConverterCatalog, Route
from magic_converter.core.settings.preferences import EncoderOptions, OutputRule, Preset, ToolOptions
from magic_converter.core.actions.tool_definitions import COMBINED_ACTIONS

if TYPE_CHECKING:
    from magic_converter.core.conversion.file_router import Action


class RequestError(ValueError):
    """A request refused before encoders or output allocators are called."""


@dataclass(frozen=True)
class PlannedFile:
    source: Path
    route: Route


@dataclass(frozen=True)
class ConversionRequest:
    action: Action
    files: tuple[PlannedFile, ...]
    converters: tuple[Converter, ...]
    preset_id: str = ""
    preset_name: str = ""
    options: EncoderOptions = EncoderOptions()
    output: OutputRule = OutputRule()
    tools: ToolOptions = ToolOptions()
    web_inputs: tuple = ()

    @property
    def combined(self) -> bool:
        return self.action.key in COMBINED_ACTIONS

    @property
    def paths(self) -> tuple[Path, ...]:
        return tuple(item.source for item in self.files)

    @property
    def uses_office_queue(self) -> bool:
        return any(item.route.uses_office_queue for item in self.files)

    def converter(self, engine_id: str) -> Converter:
        return next(converter for converter in self.converters if converter.id == engine_id)


@dataclass(frozen=True)
class DropSelection:
    paths: tuple[Path, ...]
    action_key: str
    preset: Preset | None = None
    output: OutputRule = OutputRule()
    web_inputs: tuple = ()


def prepare_request(paths: Iterable[str | Path], action_key: str, catalog: ConverterCatalog, *,
                    preset: Preset | None = None, output: OutputRule = OutputRule()) -> ConversionRequest:
    from magic_converter.core.conversion.file_router import ALL_ACTIONS, normalize_paths
    action = ALL_ACTIONS.get(action_key)
    if action is None:
        raise RequestError("알 수 없는 작업입니다")
    normalized = normalize_paths(paths)
    if not normalized:
        raise RequestError("변환할 파일을 선택하세요")
    planned = []
    for source in normalized:
        available = catalog.assess(source, action_key)
        if available.route is None:
            raise RequestError(f"{source.name}: {available.reason}")
        planned.append(PlannedFile(source, available.route))
    if preset is not None and preset.action_key != action_key:
        raise RequestError("선택한 프리셋과 실행 작업이 일치하지 않습니다.")
    if preset is not None:
        from dataclasses import replace
        action = replace(action, label=preset.name)
    return ConversionRequest(action, tuple(planned), catalog.converters,
                             preset.id if preset else "", preset.name if preset else "",
                             preset.options if preset else EncoderOptions(),
                             preset.output if preset and preset.output is not None else output,
                             preset.tools if preset else ToolOptions())
