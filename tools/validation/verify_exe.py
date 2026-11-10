"""Run the built EXE alone with Python/FFmpeg removed from its PATH."""

import hashlib
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

root = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(root))
from magic_converter import __version__
parser = argparse.ArgumentParser()
parser.add_argument("executable", nargs="?", default=str(root / "dist" / ("v" + __version__) / "magic_converter.exe"))
original = Path(parser.parse_args().executable).resolve()
destination = root / ".test-artifacts" / "단독 실행 검증"
destination.mkdir(parents=True, exist_ok=True)
executable = destination / "magic_converter.exe"
shutil.copy2(original, executable)
report = destination / "exe-self-test.json"
report.unlink(missing_ok=True)
environment = os.environ.copy()
environment["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
for key in ("MAGIC_CONVERTER_FFMPEG", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
    environment.pop(key, None)
command = [str(executable), "--self-test", str(report)]
if os.environ.get("MAGIC_CONVERTER_TEST_OFFICE") == "1":
    command.append("--self-test-office")
result = subprocess.run(command, cwd=destination, env=environment,
                        timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
if not report.is_file():
    raise SystemExit(f"EXE did not create its self-test report (exit={result.returncode})")
checks = json.loads(report.read_text(encoding="utf-8"))
from magic_converter.core.diagnostics.document_diagnostics import validate_document_report
validate_document_report(checks.get("documents", {}))
if result.returncode or checks.get("status") != "passed" or not checks.get("frozen") or checks.get("version") != __version__:
    raise SystemExit(json.dumps(checks, ensure_ascii=False, indent=2))
with original.open("rb") as artifact:
    checksum = hashlib.file_digest(artifact, "sha256").hexdigest()
print(json.dumps({"status": "passed", "exe": str(original), "bytes": original.stat().st_size,
                  "sha256": checksum,
                  "report": str(report), "checks": checks}, ensure_ascii=False, indent=2))
