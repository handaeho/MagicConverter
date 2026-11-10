"""Encoder contracts, availability and bounded conversion graph search.

The graph/preset separation is inspired by Filee (MIT); see docs/legal/THIRD_PARTY_NOTICES.md.
No UI, engine discovery, file reads or third-party imports belong in this module.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from itertools import count
from pathlib import Path
from typing import Callable, Iterable, Mapping, Protocol

from magic_converter.core.conversion.formats import FORMATS, FormatRegistry
from magic_converter.core.settings.preferences import EncoderOptions, ToolOptions

Progress = Callable[[int | None], None]
Notice = Callable[[str], None]


@dataclass(frozen=True)
class EngineStatus:
    is_available: bool = True
    reason: str = ""
    detail: str = ""
    version: str = ""


@dataclass(frozen=True)
class ConversionEdge:
    source: str
    target: str
    engine_id: str
    action_key: str
    cost: int = 10
    intent: str = "convert"
    direct_only: bool = False
    terminal_only: bool = False
    office_queue: bool = False

    def __post_init__(self) -> None:
        if not self.source or not self.target or not self.engine_id or self.cost < 0:
            raise ValueError("A conversion edge needs formats, an engine and a nonnegative cost")


@dataclass(frozen=True)
class ConversionStep:
    source: Path
    edge: ConversionEdge
    ffmpeg: str | None = None
    # Legacy adapters may publish into a private workspace instead of next to the source.
    output_source: Path | None = None
    options: EncoderOptions = EncoderOptions()
    tools: ToolOptions = ToolOptions()


class Converter(Protocol):
    id: str
    display_name: str
    edges: tuple[ConversionEdge, ...]
    status: EngineStatus
    max_parallelism: int

    def convert(self, step: ConversionStep, progress: Progress, notice: Notice) -> Path: ...


@dataclass(frozen=True)
class Route:
    steps: tuple[ConversionEdge, ...]
    cost: int

    @property
    def uses_office_queue(self) -> bool:
        return any(step.office_queue for step in self.steps)


class RoutePlanner:
    def __init__(self, edges: Iterable[ConversionEdge], *, max_steps: int = 3) -> None:
        if max_steps < 1:
            raise ValueError("max_steps must be positive")
        self.max_steps = max_steps
        self._edges: dict[str, list[ConversionEdge]] = {}
        for edge in edges:
            self._edges.setdefault(edge.source, []).append(edge)

    def plan(self, source: str, target: str, *, intent: str = "convert") -> Route | None:
        # Identity operations (re-encoding, stripping, compressing) need an explicit edge.
        # A cycle is never accepted as a same-format conversion.
        serial = count()
        queue: list[tuple[int, int, int, str, tuple[ConversionEdge, ...]]] = [(0, 0, next(serial), source, ())]
        distances = {(source, 0): 0}
        while queue:
            cost, hops, _, node, steps = heapq.heappop(queue)
            if distances.get((node, hops)) != cost:
                continue
            if steps and node == target:
                return Route(steps, cost)
            if hops == self.max_steps:
                continue
            for edge in self._edges.get(node, ()):
                if edge.intent != intent:
                    continue
                if source == target and (hops or edge.target != target):
                    continue
                if edge.source == edge.target and not (hops == 0 and source == target):
                    continue
                if edge.direct_only and (hops != 0 or edge.target != target):
                    continue
                if edge.terminal_only and edge.target != target:
                    continue
                # Do not revisit a format: a round trip cannot improve fidelity.
                if edge.target in (source, *(step.target for step in steps)) and edge.target != target:
                    continue
                state, candidate = (edge.target, hops + 1), cost + edge.cost
                if candidate >= distances.get(state, float("inf")):
                    continue
                distances[state] = candidate
                heapq.heappush(queue, (candidate, hops + 1, next(serial), edge.target, (*steps, edge)))
        return None


@dataclass(frozen=True)
class ActionPolicy:
    target: str | None
    sources: frozenset[str] | None = None
    intent: str = "convert"
    batch: bool = False


@dataclass(frozen=True)
class Availability:
    route: Route | None
    reason: str = ""


class ConverterCatalog:
    def __init__(self, converters: Iterable[Converter] = (), *,
                 policies: Mapping[str, ActionPolicy] | None = None,
                 formats: FormatRegistry = FORMATS) -> None:
        self.formats = formats
        self._converters: dict[str, Converter] = {}
        self._policies = dict(policies or {})
        self._planners: dict[bool, RoutePlanner] = {}
        for converter in converters:
            self.register(converter)

    @property
    def converters(self) -> tuple[Converter, ...]:
        return tuple(self._converters.values())

    def register(self, converter: Converter) -> None:
        if not converter.id or converter.id in self._converters:
            raise ValueError(f"Duplicate or empty engine id: {converter.id}")
        for edge in converter.edges:
            if edge.engine_id != converter.id:
                raise ValueError(f"Edge belongs to another engine: {edge.engine_id}")
            self.formats.get(edge.source)
            self.formats.get(edge.target)
        self._converters[converter.id] = converter
        self._planners.clear()

    def policy(self, action_key: str) -> ActionPolicy | None:
        return self._policies.get(action_key)

    def planner(self, *, include_unavailable: bool = False) -> RoutePlanner:
        if include_unavailable not in self._planners:
            self._planners[include_unavailable] = RoutePlanner(
                edge for converter in self.converters
                if include_unavailable or converter.status.is_available for edge in converter.edges)
        return self._planners[include_unavailable]

    def assess(self, source: str | Path, action_key: str) -> Availability:
        policy = self.policy(action_key)
        if policy is None:
            return Availability(None, "알 수 없는 작업입니다")
        if policy.batch:
            return Availability(Route((), 0))
        file_format = self.formats.detect(source)
        if file_format is None or not file_format.readable:
            return Availability(None, "지원하지 않는 입력 형식입니다")
        if policy.sources is not None and file_format.id not in policy.sources:
            return Availability(None, "선택한 모든 파일에 적용할 수 없습니다")
        target = policy.target or file_format.id
        route = self.planner().plan(file_format.id, target, intent=policy.intent)
        if route is not None:
            return Availability(route)
        possible = self.planner(include_unavailable=True).plan(file_format.id, target, intent=policy.intent)
        if possible is not None:
            reasons = dict.fromkeys(self._converters[edge.engine_id].status.reason
                                   or f"{self._converters[edge.engine_id].display_name} 사용 불가"
                                   for edge in possible.steps
                                   if not self._converters[edge.engine_id].status.is_available)
            return Availability(None, " / ".join(reasons))
        return Availability(None, "지원하는 변환 경로가 없습니다")
