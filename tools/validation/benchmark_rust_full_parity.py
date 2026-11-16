"""Measure matched, standalone Rust CLI routes after differential validation."""

import argparse
import hashlib
import json
from pathlib import Path
import random
import statistics
import sys
import tarfile
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_large_input_rss import measure
from tools.validation.verify_rust_full_parity import digest

SEQUENCE = ("python", "rust", "rust", "python") * 2
MIB = 1024**2
ENGINE_PROGRAM = """import json, os, sys, tempfile, time
from pathlib import Path
from magic_converter.core.archives.archive_converter import convert_archive
source, target, destination = sys.argv[1:]
with tempfile.TemporaryDirectory(dir=Path(destination).parent) as staging:
    path = Path(staging) / "archive"
    start = time.perf_counter()
    convert_archive(Path(source), path, "." + target)
    elapsed = time.perf_counter() - start
    os.rename(path, destination)
print(json.dumps({"ok": True, "jobs": [{"outputs": [destination], "engine_s": elapsed}]}))
"""


def cases(folder, large_copy):
    folder.mkdir()
    small = tuple(folder / f"한글 {index:02d}.txt" for index in range(32))
    for path in small:
        path.write_bytes(bytes(range(256)) * 16)
    rng = random.Random(20261112)
    payloads = (folder / "compressible.txt", folder / "random.dat")
    with payloads[0].open("wb") as stream:
        chunk = b"MagicConverter" * (MIB // len(b"MagicConverter"))
        for _ in range(32):
            stream.write(chunk)
    with payloads[1].open("wb") as stream:
        for _ in range(32):
            stream.write(rng.randbytes(MIB))
    tar = folder / "archive.tar"
    with tarfile.open(tar, "w", format=tarfile.PAX_FORMAT) as archive:
        for path in payloads:
            entry = archive.gettarinfo(path, arcname=path.name)
            entry.mtime = 1_700_000_001
            with path.open("rb") as stream:
                archive.addfile(entry, stream)
    packed = folder / "archive.zip"
    with zipfile.ZipFile(packed, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in payloads:
            archive.write(path, arcname=path.name)
    archive_members = [(path.name, path.stat().st_size, digest(path)) for path in payloads]
    return {
        "copy-small-32": (small, "tool:copy_original", None),
        "copy-large": ((large_copy,), "tool:copy_original", None),
        "zip-to-tar": ((packed,), "archive:tar", archive_members),
        "tar-to-zip": ((tar,), "archive:zip", archive_members),
        "tar-to-tar-gz": ((tar,), "archive:gzip", archive_members),
    }


def signature(output, action):
    if action == "archive:zip":
        with zipfile.ZipFile(output) as archive:
            return [(entry.filename, entry.file_size, hashlib.file_digest(archive.open(entry), "sha256").hexdigest())
                    for entry in archive.infolist()]
    with tarfile.open(output) as archive:
        return [(entry.name, entry.size, hashlib.file_digest(archive.extractfile(entry), "sha256").hexdigest())
                for entry in archive]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--large-copy", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--include-engine", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    commands = {"python": [sys.executable, "-B", str(args.baseline.resolve(strict=True) / "run_magic_converter.py")],
                "rust": [str(args.worker.resolve(strict=True))]}
    inputs = cases(root / "inputs", args.large_copy.resolve(strict=True))
    fingerprints = {path: (path.stat().st_size, digest(path)) for paths, _, _ in inputs.values() for path in paths}
    evidence = {"status": "running", "sequence": SEQUENCE,
                "worker_sha256": digest(Path(commands["rust"][0])), "runs": [], "summary": {}}

    def save():
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    save()
    try:
        workloads = list(inputs.items())
        if args.include_engine:
            workloads += [(f"engine-{case}", workload) for case, workload in inputs.items() if workload[2] is not None]
        for case, (paths, action, archive_members) in workloads:
            summary = {}
            for index, edition in enumerate(("python", "rust", *SEQUENCE)):
                folder = root / case / f"{index:02d}-{edition}"
                folder.mkdir(parents=True)
                if case.startswith("engine-"):
                    target = {"archive:zip": "zip", "archive:tar": "tar", "archive:gzip": "tar.gz"}[action]
                    output = folder / f"output.{target}"
                    if edition == "python":
                        program = f"import sys; sys.path.insert(0, {str(args.baseline.resolve())!r})\n" + ENGINE_PROGRAM
                        cmd = [sys.executable, "-B", "-c", program, str(paths[0]), target, str(output)]
                    else:
                        cmd = [*commands[edition], "archive-engine", str(paths[0]), target, str(output)]
                else:
                    cmd = [*commands[edition], "convert", *map(str, paths), "--action", action,
                           "--output", str(folder / "output"), "--data-dir", str(folder / "profile"), "--timeout", "150", "--json"]
                result = measure(cmd, folder / "measure", expected_ok=True, deadline=180)
                outputs = result["outputs"]
                if archive_members is not None:
                    output, = outputs
                    assert signature(Path(output["path"]), action) == archive_members
                else:
                    assert [(out["bytes"], out["sha256"]) for out in outputs] == [fingerprints[path] for path in paths]
                record = {"case": case, "edition": edition, "warmup": index < 2,
                          "input_bytes": sum(fingerprints[path][0] for path in paths),
                          "output_bytes": sum(out["bytes"] for out in outputs),
                          "elapsed_s": (result["clock_ns"]["tree_exit_observed"] - result["clock_ns"]["launch"]) / 1e9,
                          "sampled_rss_bytes": result["sampled_tree_peak_rss_bytes"],
                          "peak_job_commit_bytes": result["job_peak_commit_bytes"],
                          "io": result["job_accounting"]["IoInfo"]}
                if case.startswith("engine-"):
                    record["engine_s"] = result["jobs"][0]["engine_s"]
                evidence["runs"].append(record)
                for output in outputs:
                    Path(output["path"]).unlink()
                save()
            for edition in commands:
                rows = [row for row in evidence["runs"] if row["case"] == case and row["edition"] == edition and not row["warmup"]]
                summary[edition] = {metric: {"median": statistics.median(values), "min": min(values), "max": max(values)}
                                    for metric in ("elapsed_s", "sampled_rss_bytes", "output_bytes", "engine_s")
                                    if metric in rows[0] and (values := [row[metric] for row in rows])}
            evidence["summary"][case] = summary
            print(case, flush=True)
        assert all(digest(path) == fingerprint[1] for path, fingerprint in fingerprints.items())
        evidence["status"] = "passed"
    except Exception as error:
        evidence.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
