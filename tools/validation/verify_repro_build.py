"""Build a Git-listed source snapshot in an empty venv with network disabled.

The original working tree is not reset or cleaned. All outputs live under the
caller-selected report directory. A mismatch fails; it is not called reproducible.
"""

import argparse
import datetime
import uuid
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter import __version__
from tools.validation.validation_environment import check_environment


def sha(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()


def write_result(output, result):
    temporary = output / ("result-" + result["run_id"] + ".tmp")
    try:
        temporary.write_text(json.dumps(result, indent=2) + "\n", encoding="utf-8")
        temporary.replace(output / "result.json")
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--work-path", type=Path, default=Path("build/release"),
                        help="Same relative build.ps1 -WorkPath used for the original build")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to((ROOT / ".test-artifacts").resolve()):
        raise ValueError("Reproduction output must stay under .test-artifacts")
    output.mkdir(parents=True, exist_ok=True)
    result = {
        "run_id": uuid.uuid4().hex,
        "started_utc": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "status": "running", "offline": True, "fresh_venv": True, "compared": [],
        "work_path": args.work_path.as_posix(),
    }
    # Invalidate any earlier success before even asking Git for the inputs.
    write_result(output, result)
    try:
        if (args.work_path.is_absolute() or ".." in args.work_path.parts
                or not args.work_path.parts or args.work_path.parts[0] != "build"):
            raise ValueError("Build work path must stay relative under build/")
        result['environment_preflight'] = check_environment()
        write_result(output, result)
        reproduce(output, result)
    except Exception as exc:
        result.update(status="failed", error=str(exc), error_type=type(exc).__name__)
    result["finished_utc"] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    write_result(output, result)
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


def reproduce(output, result):
    # Qt contains long resource filenames. Keep the fresh tree shallow even
    # when the report directory is deeply nested; no OS/registry change needed.
    source = Path(tempfile.mkdtemp(prefix="r-", dir=ROOT / ".test-artifacts"))
    if not source.resolve().is_relative_to(ROOT / ".test-artifacts"):
        raise ValueError("Unexpected reproduction source directory")
    result["source_directory"] = str(source)
    names = (
        subprocess.check_output(
            ["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
            cwd=ROOT,
        )
        .decode("utf-8")
        .split("\0")
    )
    # A source reorganization can leave unstaged old paths in the Git index.
    # Honor tracked deletions so the snapshot represents the current worktree.
    deleted = set(
        subprocess.check_output(["git", "ls-files", "-z", "--deleted"], cwd=ROOT)
        .decode("utf-8")
        .split("\0")
    )
    inputs = {}
    for name in sorted(set(filter(None, names)) - deleted):
        original = ROOT / name
        if (
            not original.is_file()
            or original.is_symlink()
            or not original.resolve().is_relative_to(ROOT)
        ):
            raise ValueError("Invalid Git-listed source: " + name)
        target = source / name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(original, target)
        inputs[name] = sha(original)
    (output / "source-inputs.json").write_text(
        json.dumps(inputs, sort_keys=True, indent=2), encoding="utf-8"
    )
    python = source / ".venv/Scripts/python.exe"
    subprocess.run(
        [sys._base_executable, "-m", "venv", str(source / ".venv")], check=True
    )
    environment = os.environ.copy()
    for key in (
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "MAGIC_CONVERTER_TEST_OFFICE",
    ):
        environment.pop(key, None)
    environment["PYTHONUTF8"] = "1"
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["MAGIC_CONVERTER_PROFILE_DIR"] = str(output / "profile")
    command = [
        "powershell.exe",
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(source / "build.ps1"),
        "-Offline",
        "-BuildPython",
        str(python),
        "-Wheelhouse",
        str(ROOT / ".build-cache/wheels"),
        "-VendorCache",
        str(ROOT / ".build-cache/vendor"),
        "-WorkPath",
        result.get("work_path", "build/release"),
    ]
    with (output / "build.log").open("wb") as log:
        process = subprocess.run(
            command, cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT
        )
    result.update({
        "status": "failed",
        "offline": True,
        "fresh_venv": True,
        "build_exit": process.returncode,
        "compared": [],
        "source_directory": str(source),
    })
    if process.returncode == 0:
        original_dir = ROOT / "dist" / ("v" + __version__)
        reproduced_dir = source / "dist" / ("v" + __version__)
        for name in (
            "magic_converter.exe",
            "magic_converter-cli.exe",
            "sbom.spdx.json",
            "build-inputs.json",
        ):
            a, b = sha(original_dir / name), sha(reproduced_dir / name)
            result["compared"].append(
                {"file": name, "original": a, "reproduced": b, "identical": a == b}
            )
        with (output / "source-self-test.log").open("wb") as log:
            test = subprocess.run(
                [
                    str(python),
                    "run_magic_converter.py",
                    "--self-test",
                    str(output / "source-self-test.json"),
                ],
                cwd=source,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                timeout=180,
            )
        result["source_self_test_exit"] = test.returncode
        result["status"] = (
            "passed"
            if test.returncode == 0 and all(p["identical"] for p in result["compared"])
            else "failed"
        )



if __name__ == "__main__":
    raise SystemExit(main())
