"""Opt-in monotonic timestamps for local conversion benchmarks."""
import json
import os
from pathlib import Path
from time import perf_counter_ns


def enabled():
    return bool(os.environ.get("MAGIC_CONVERTER_PHASE_TRACE"))


def mark(phase, **metrics):
    directory = os.environ.get("MAGIC_CONVERTER_PHASE_TRACE")
    if not directory:
        return
    pid = os.getpid()
    event = {"phase": phase, "ns": perf_counter_ns(), "pid": pid, **metrics}
    try:
        with (Path(directory) / f"{pid}.jsonl").open("a", encoding="ascii") as stream:
            stream.write(json.dumps(event) + "\n")
    except OSError:
        # Diagnostics must not turn a successful conversion into a failure.
        return
