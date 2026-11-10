"""Public conversion results and progress, independent of worker implementation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, TYPE_CHECKING

from magic_converter.core.conversion.file_router import Action

if TYPE_CHECKING:
    from magic_converter.core.conversion.conversion_request import ConversionRequest

FileState = Literal["queued", "running", "done", "failed", "cancelled", "timed_out"]


@dataclass(frozen=True)
class FileResult:
    source: Path
    state: FileState
    outputs: tuple[Path, ...] = ()
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    cancel_requested: bool = False


@dataclass(frozen=True)
class JobSnapshot:
    job_id: int
    action: Action
    files: tuple[FileResult, ...]


@dataclass
class JobResult:
    job_id: int
    action: Action
    total: int
    outputs: list[Path] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    completed: int = 0
    warnings: list[str] = field(default_factory=list)
    cancelled: bool = False
    timed_out: bool = False
    files: list[FileResult] = field(default_factory=list)
    request: ConversionRequest | None = None
    started_at: str = ""
    elapsed_seconds: float = 0


@dataclass(frozen=True)
class JobProgress:
    job_id: int
    action_label: str
    filename: str
    file_index: int
    total_files: int
    percent: int | None
    phase: str = "변환 중"
    aggregate: bool = False
    batch_percent: int | None = None
    finished_files: int = 0
    running_files: int = 0

    @property
    def overall_percent(self) -> int | None:
        if self.aggregate:
            return self.batch_percent
        if self.percent is None:
            return None
        return min(100, ((self.file_index - 1) * 100 + self.percent) // max(1, self.total_files))
