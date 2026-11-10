"""Stage-7 engines share atomic output publication and existing cancellation."""

from magic_converter.core.conversion.converter import output_file
from magic_converter.core.conversion.formats import FORMATS
from magic_converter.core.jobs.job_control import check_cancelled, report_stage

ENGINES = frozenset({"data", "font", "ebook", "email", "mesh", "geo"})


def execute(step, progress, notice):
    source, target = step.edge.source, step.edge.target
    report_stage(f"{step.edge.engine_id} · {source.upper()} → {target.upper()}")
    progress(0)
    from magic_converter.core.jobs.specialized_worker import convert_isolated
    check_cancelled()
    with output_file(step.output_source or step.source, FORMATS.get(target).suffix) as (
        temporary,
        destination,
    ):
        convert_isolated(step, temporary, progress, notice)
        check_cancelled()
    progress(100)
    return destination
