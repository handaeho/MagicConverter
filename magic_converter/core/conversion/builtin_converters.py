"""Register existing encoders without loading Qt or probing the user's PC."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from magic_converter.core.conversion.conversion_catalog import (ActionPolicy, ConversionEdge, ConversionStep, ConverterCatalog,
                                 EngineStatus, Notice, Progress)
from magic_converter.core.conversion.formats import FORMATS


@dataclass(frozen=True)
class BuiltInConverter:
    id: str
    display_name: str
    edges: tuple[ConversionEdge, ...]
    status: EngineStatus = EngineStatus()
    max_parallelism: int = 2

    def convert(self, step: ConversionStep, progress: Progress, notice: Notice) -> Path:
        from magic_converter.core.conversion.conversion_execution import run_legacy_action
        from magic_converter.core.jobs.job_control import supervised_api
        with supervised_api():
            return run_legacy_action(step, progress, notice)




def runtime_catalog(ffmpeg: str | None) -> ConverterCatalog:
    pass
    pass
    from magic_converter.core.engines.office_engine import presentation_available
    from magic_converter.core.engines.rhwp_engine import locate
    pass
