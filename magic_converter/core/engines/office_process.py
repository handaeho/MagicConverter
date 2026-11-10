"""Bounded Office helper supervision; no inherited pipe can hold wait open."""

import json
import logging
import os
import subprocess
import tempfile
import time
from pathlib import Path

from magic_converter.core.jobs.job_control import JobTimedOut, check_cancelled, report_stage
from magic_converter.core.jobs.process_runner import start_process, stop_process
from magic_converter.core.engines.office_budget import OFFICE_COMMIT_BYTES

LOGGER = logging.getLogger(__name__)


def run_helper(command: list[str], state_path: Path, *, timeout: float = 180) -> tuple[int, str]:
    started = stage_started = time.monotonic()
    stage, stamp, detail = "starting", None, ""
    stage_timeout = min(30, timeout)
    with tempfile.TemporaryFile() as errors:
        process, job = start_process(command, stdout=subprocess.DEVNULL, stderr=errors,
                                     memory_limit=OFFICE_COMMIT_BYTES)
        try:
            while True:
                check_cancelled()
                code = process.poll()
                if code is not None:
                    errors.seek(0, os.SEEK_END)
                    errors.seek(max(0, errors.tell() - 2400))
                    return code, errors.read().decode("utf-8", errors="replace")
                now = time.monotonic()
                try:
                    state = json.loads(state_path.read_text(encoding="utf-8"))
                    detail = str(state.get("detail", ""))[:160]
                    if state.get("stamp") != stamp:
                        stamp, stage = state.get("stamp"), state.get("stage", "starting")
                        stage_started = now
                        stage_timeout, message = STAGES.get(stage, (90, "문서 변환 중"))
                        if stage=='office_export':
                            # A large workbook can legitimately print for
                            # longer than activation/open. The absolute helper
                            # deadline still caps this stage and cannot renew.
                            stage_timeout=timeout
                        report_stage(message)
                except (OSError, ValueError):
                    pass
                if now - started > timeout or now - stage_started > stage_timeout:
                    suffix = f" ({detail})" if detail else ""
                    raise JobTimedOut(f"{STAGES.get(stage, (0, 'Office 작업'))[1]} 단계에서 응답 시간이 초과되었습니다.{suffix}")
                time.sleep(0.15)
        finally:
            stop_process(process, job)
