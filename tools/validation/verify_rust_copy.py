"""Compare the Rust copy worker and the Python copier on the Windows host."""

import argparse
from concurrent.futures import ThreadPoolExecutor
from functools import partial
import hashlib
import json
import os
from pathlib import Path
import platform
import random
import statistics
import subprocess
import sys
from threading import Event
import time
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from magic_converter.core.conversion import conversion_execution as route
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs import phase_trace, process_runner
from magic_converter.core.jobs.job_control import Control, JobCancelled, JobTimedOut, current
from magic_converter.core.settings.preferences import OutputRule
from tools.validation.rust_copy_worker import copy_with_worker


SEQUENCE = ("python", "rust", "rust", "python") * 2
SUPERVISOR_SEQUENCE = ("python", "rust-poll", "rust", "rust", "rust-poll", "python",
                       "rust", "python", "rust-poll", "rust-poll", "python", "rust")
MIB = 1024**2


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fixtures(folder, *, extended=False):
    folder.mkdir()
    empty = folder / "빈 파일.txt"
    empty.touch()
    compound = folder / "한글 🪄.TAR.GZ"
    compound.write_bytes(bytes(range(256)) * 16)
    many = tuple(folder / f"작은 파일 {index:02d}.txt" for index in range(32))
    for path in many:
        path.write_bytes(path.name.encode("utf-8") * 128)
    rng = random.Random(20261112)
    large = folder / "large.wav"
    with large.open("wb") as stream:
        for _ in range(128):
            stream.write(rng.randbytes(MIB))
    patterned = folder / "compressible.txt"
    with patterned.open("wb") as stream:
        block = b"ABCDEFGH" * (MIB // 8)
        for _ in range(32):
            stream.write(block)
    packed = folder / "already-compressed.zip"
    with zipfile.ZipFile(packed, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("random.bin", rng.randbytes(4 * MIB))
    inputs = {"empty": (empty,), "unicode_compound": (compound,), "small_batch": many,
              "large": (large,), "compressible": (patterned,), "compressed": (packed,)}
    if extended:
        payload = large.read_bytes()
        for repetitions in (4, 8):
            size = 128 * repetitions
            path = folder / f"{size}MiB.wav"
            with path.open("wb") as stream:
                for _ in range(repetitions):
                    stream.write(payload)
            inputs[f"large_{size}"] = (path,)
    return inputs


def copy_child(scope, edition, worker, output, sources):
    events = []

    def mark(phase, **metrics):
        events.append({"phase": phase, "ns": time.perf_counter_ns(), **metrics})

    mark("python_entry")
    try:
        return copy_child_measured(scope, edition, worker, output, sources, mark)
    finally:
        (output.parent / "trace" / "timing.json").write_text(json.dumps(events), encoding="ascii")


def copy_child_measured(scope, edition, worker, output, sources, mark):
    perf_counter_ns = time.perf_counter_ns
    python_copy = route.copy_file

    def measured_python(source, target):
        started = perf_counter_ns()
        python_copy(source, target)
        completed = perf_counter_ns()
        mark("copy_payload", stream_ns=completed - started, active_start_ns=started,
             active_end_ns=completed, payload_bytes=target.stat().st_size)

    if scope == "app":
        from magic_converter.main import main

        def measured_rust(source, target):
            mark("copy_payload", **copy_with_worker(worker, source, target, wake_on_exit=edition == "rust"))

        writer = measured_python if edition == "python" else measured_rust
        with patch.object(route, "copy_file", writer), patch.object(phase_trace, "mark", mark):
            return main(["convert", *map(str, sources), "--action", "tool:copy_original",
                         "--output", str(output), "--data-dir", str(output.parent / "profile"),
                         "--timeout", "180", "--json"])

    output.mkdir()
    outputs = []
    mark("conversion_start")
    for source in sources:
        target = output / source.name
        if edition == "python":
            measured_python(source, target)
        else:
            started = perf_counter_ns()
            child = subprocess.Popen([worker, source, target], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            launched = perf_counter_ns()
            stdout, stderr = child.communicate()
            completed = perf_counter_ns()
            if child.returncode:
                raise RuntimeError(stderr.decode("utf-8", errors="replace"))
            kind, length, elapsed = stdout.splitlines()[-1].split()
            assert kind == b"copied" and int(length) == target.stat().st_size
            mark("copy_payload", stream_ns=int(elapsed), supervised_ns=completed - started,
                 launch_ns=launched - started, validate_ns=perf_counter_ns() - completed,
                 active_start_ns=started, active_end_ns=completed, payload_bytes=int(length))
        outputs.append(str(target))
    mark("conversion_complete")
    print(json.dumps({"ok": True, "jobs": [{"outputs": outputs}]}))
    return 0


def transactions(root, worker, source):
    root.mkdir()
    baseline_hash = digest(source)
    evidence = {}
    for edition in ("python", "rust"):
        folder = root / edition
        folder.mkdir()
        published = folder / "published"
        rule = OutputRule(location="folder", directory=str(published))
        existing = published / (source.stem + "_copy" + source.suffix)
        published.mkdir()
        existing.write_bytes(b"existing user data")

        def run_copy(output_rule=rule):
            return route._copy_original(source, source, output_rule)

        writer = route.copy_file if edition == "python" else partial(copy_with_worker, worker)
        with patch.object(route, "copy_file", writer):
            same_name = OutputRule(template="{stem}", collision="error")
            error_rule = OutputRule(location="folder", directory=str(published), collision="error")
            for requested in (same_name, error_rule):
                try:
                    run_copy(requested)
                except ConversionError:
                    pass
                else:
                    raise AssertionError("Existing source or output was not protected")
            with ThreadPoolExecutor(max_workers=3) as pool:
                copies = list(pool.map(lambda _: run_copy(), range(3)))
            assert len(set(copies)) == 3 and all(digest(path) == baseline_hash for path in copies)
            before = set(published.iterdir())

            for error in (JobCancelled, JobTimedOut):
                control = Control(Event(), lambda _: None)

                def abort(_=None):
                    if error is JobCancelled:
                        control.event.set()
                    else:
                        control.deadline = time.monotonic() - 1

                def interrupted(src, target):
                    if edition == "rust":
                        copy_with_worker(worker, src, target, abort)
                    else:
                        writer(src, target)
                        abort()

                token = current.set(control)
                try:
                    with patch.object(route, "copy_file", interrupted):
                        run_copy()
                except error:
                    pass
                else:
                    raise AssertionError(f"{error.__name__} was not propagated")
                finally:
                    current.reset(token)
                assert set(published.iterdir()) == before

            if edition == "rust":
                original_start = process_runner.start_process

                def terminated(*args, **kwargs):
                    process, job = original_start(*args, **kwargs)
                    process.kill()
                    return process, job

                with patch.object(process_runner, "start_process", terminated):
                    try:
                        run_copy()
                    except ConversionError:
                        pass
                    else:
                        raise AssertionError("Terminated worker published a result")
                assert set(published.iterdir()) == before

            retry = run_copy()
            assert digest(retry) == baseline_hash
            assert existing.read_bytes() == b"existing user data" and digest(source) == baseline_hash
            evidence[edition] = {"concurrent_names": sorted(path.name for path in copies),
                                 "retry": retry.name, "original_and_existing_unchanged": True,
                                 "cancel_and_timeout_rolled_back": True, "terminated_worker_rolled_back": edition == "rust"}
        for path in (*copies, retry):
            path.unlink()
    def refuse(destination):
        failure = subprocess.run([worker, source, destination], capture_output=True, check=False)
        assert failure.returncode and digest(destination) == baseline_hash

    refuse(source)
    evidence["rust"]["direct_same_file_rejected"] = True
    alias = root / "alias.wav"
    try:
        os.link(source, alias)
    except PermissionError as error:
        evidence["rust"]["hard_link"] = f"unavailable: {error}"
    else:
        refuse(alias)
        alias.unlink()
        evidence["rust"]["hard_link"] = "rejected without changing source"
    return evidence


def windows_metadata(root, worker, baseline):
    import ntsecuritycon
    import win32security as security

    root.mkdir()
    source = root / "metadata.txt"
    source.write_bytes(b"primary data")
    alternate = ":magic-copy-test"
    Path(str(source) + alternate).write_bytes(b"alternate data")
    stamp = 1_700_000_001_123_456_700
    os.utime(source, ns=(stamp, stamp))
    flag = security.DACL_SECURITY_INFORMATION
    marker = security.CreateWellKnownSid(security.WinNullSid, None)

    def dacl(path):
        return security.GetNamedSecurityInfo(str(path), security.SE_FILE_OBJECT, flag).GetSecurityDescriptorDacl()

    def has_marker(path):
        acl = dacl(path)
        return any(acl.GetAce(i)[-1] == marker for i in range(acl.GetAceCount()))

    acl = dacl(source)
    acl.AddAccessAllowedAce(security.ACL_REVISION, ntsecuritycon.FILE_READ_DATA, marker)
    security.SetNamedSecurityInfo(str(source), security.SE_FILE_OBJECT,
                                 flag | security.PROTECTED_DACL_SECURITY_INFORMATION, None, None, acl, None)
    source_acl_verified = has_marker(source)
    if not source_acl_verified:
        print("Windows did not retain the fixture ACL marker", flush=True)
    before = digest(source), source.stat().st_mtime_ns
    assert before[1] == stamp

    results = {}
    for edition in ("2.2.39", "python", "rust"):
        destination = root / edition
        if edition == "2.2.39":
            program = ("import sys; from pathlib import Path;"
                       "from magic_converter.core.conversion.conversion_execution import _copy_original;"
                       "from magic_converter.core.settings.preferences import OutputRule;"
                       "source = Path(sys.argv[1]);"
                       "_copy_original(source, source, OutputRule(location='folder', directory=sys.argv[2]))")
            subprocess.run([sys.executable, "-B", "-c", program, source, destination], cwd=baseline, check=True)
        else:
            writer = route.copy_file if edition == "python" else partial(copy_with_worker, worker)
            with patch.object(route, "copy_file", writer):
                route._copy_original(source, source, OutputRule(location="folder", directory=str(destination)))
        output = destination / "metadata_copy.txt"
        assert digest(output) == before[0] and output.stat().st_mtime_ns != stamp
        try:
            Path(str(output) + alternate).read_bytes()
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("Alternate stream was unexpectedly copied")
        if source_acl_verified:
            assert not has_marker(output)
        results[edition] = {"output_mtime_ns": output.stat().st_mtime_ns,
                            "alternate_stream_present": False,
                            "source_acl_marker_present": False if source_acl_verified else None}
    assert (digest(source), source.stat().st_mtime_ns) == before
    assert Path(str(source) + alternate).read_bytes() == b"alternate data"
    if source_acl_verified:
        assert has_marker(source)
    return {"source_mtime_ns": stamp, "source_acl_verified": source_acl_verified, "results": results}


def active_seconds(payloads):
    """Measure the union of active copy intervals, including parallel requests."""
    end = total = 0
    for start, stop in sorted((item["active_start_ns"], item["active_end_ns"]) for item in payloads):
        total += max(0, stop - max(start, end))
        end = max(end, stop)
    return total / 1e9


def run_matrix(root, worker, baseline, *, compare_supervisors=False):
    from tools.validation.verify_large_input_rss import measure

    inputs = fixtures(root / "inputs", extended=compare_supervisors)
    sequences = {"app": SUPERVISOR_SEQUENCE} if compare_supervisors else {"engine": SEQUENCE, "app": SEQUENCE}
    metadata = {path: (digest(path), path.stat().st_size) for paths in inputs.values() for path in paths}
    records = []
    report = {"status": "running", "sequences": sequences, "worker_sha256": digest(worker),
              "python": sys.version, "windows": platform.platform(),
              "inputs": {key: [{"name": path.name, "sha256": metadata[path][0], "bytes": metadata[path][1]}
                               for path in paths] for key, paths in inputs.items()},
              "runs": records}

    def save():
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    save()
    try:
        report["windows_copy_metadata"] = windows_metadata(root / "windows-metadata", worker, baseline)
        report["transactions"] = transactions(root / "transactions", worker, inputs["large"][0])
        for scope, sequence in sequences.items():
            warmups = tuple(dict.fromkeys(sequence))
            for case, sources in inputs.items():
                for index, edition in enumerate((*warmups, *sequence)):
                    trial = root / scope / case / f"{index:02d}-{edition}"
                    trace = trial / "trace"
                    trace.mkdir(parents=True)
                    output_folder = trial / "output"
                    staging_peak = 0

                    def sample_staging():
                        nonlocal staging_peak
                        staged = (*output_folder.glob(".magic_converter-*"),
                                  *output_folder.glob(".magic-copy-*/payload"))
                        size = 0
                        for path in staged:
                            try:
                                size += path.stat().st_size
                            except FileNotFoundError:
                                pass  # Publication can move a file during the sample.
                        staging_peak = max(staging_peak, size)

                    args = [sys.executable, "-X", "utf8", str(Path(__file__).resolve()), "--child",
                            scope, edition, str(worker), str(output_folder), *map(str, sources)]
                    measurement = measure(args, trial / "measure", expected_ok=True, deadline=180,
                                          on_sample=sample_staging if scope == "app" else None)
                    events = json.loads((trace / "timing.json").read_bytes())
                    milestones = {event["phase"]: event["ns"] for event in events}
                    payloads = [event for event in events if event["phase"] == "copy_payload"]
                    assert len(payloads) == len(sources) == len(measurement["outputs"])
                    names = []
                    for source, output in zip(sources, measurement["outputs"], strict=True):
                        target = Path(output["path"])
                        assert (output["sha256"], output["bytes"]) == metadata[source]
                        suffix = "".join(source.suffixes[-2:]) if source.name.lower().endswith(".tar.gz") else source.suffix
                        expected = source.name if scope == "engine" else source.name[:-len(suffix)] + "_copy" + suffix
                        assert target.name == expected and target.parent == trial / "output"
                        names.append(target.name)
                        target.unlink()
                    started = milestones["conversion_start"]
                    complete = milestones["conversion_complete"]
                    account = measurement["job_accounting"]["IoInfo"]
                    entry = {"scope": scope, "case": case, "edition": edition, "warmup": index < len(warmups),
                             "total_s": (measurement["clock_ns"]["tree_exit_observed"]
                                         - measurement["clock_ns"]["launch"]) / 1e9,
                             "app_startup_s": (started - measurement["clock_ns"]["launch"]) / 1e9,
                             "request_to_publication_s": (complete - started) / 1e9,
                             "sampled_staging_peak_bytes": staging_peak,
                             "output_bytes": sum(value["payload_bytes"] for value in payloads),
                             "output_names": names, "peak_sampled_rss_bytes": measurement["sampled_tree_peak_rss_bytes"],
                             "peak_job_commit_bytes": measurement["job_peak_commit_bytes"],
                             "job_read_bytes": account["ReadTransferCount"], "job_write_bytes": account["WriteTransferCount"]}
                    for phase in ("stream_ns", "supervised_ns", "launch_ns", "validate_ns"):
                        if phase in payloads[0]:
                            entry[phase.replace("_ns", "_s")] = sum(event[phase] for event in payloads) / 1e9
                    if "supervised_s" in entry:
                        entry["process_overhead_s"] = entry["supervised_s"] - entry["stream_s"]
                    entry["active_copy_s"] = active_seconds(payloads)
                    entry["request_other_s"] = entry["request_to_publication_s"] - entry["active_copy_s"]
                    assert entry["request_other_s"] >= 0
                    records.append(entry)
                    save()
                print(f"{scope} {case}: verified", flush=True)
        assert all(digest(path) == fingerprint[0] for path, fingerprint in metadata.items())
        report["summary"] = []
        for scope, sequence in sequences.items():
            for case in inputs:
                for edition in dict.fromkeys(sequence):
                    group = [row for row in records if not row["warmup"] and
                             (row["scope"], row["case"], row["edition"]) == (scope, case, edition)]
                    summary = {"scope": scope, "case": case, "edition": edition}
                    for metric in ("total_s", "request_to_publication_s", "stream_s",
                                   "peak_sampled_rss_bytes", "peak_job_commit_bytes"):
                        values = [row[metric] for row in group]
                        summary[metric] = {"median": statistics.median(values), "min": min(values), "max": max(values)}
                    report["summary"].append(summary)
        report.update(status="passed", original_sha256_unchanged=True)
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()


def main():
    if sys.argv[1:2] == ["--child"]:
        scope, edition, worker, output, *sources = sys.argv[2:]
        return copy_child(scope, edition, Path(worker), Path(output), list(map(Path, sources)))
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--compare-supervisors", action="store_true",
                        help="Compare both Rust supervisors and Python through the CLI, with inputs up to 1 GiB")
    args = parser.parse_args()
    root = args.output.resolve()
    if os.name != "nt" or sys.flags.optimize or not root.is_relative_to(ROOT / ".test-artifacts"):
        parser.error("Use Windows with assertions enabled and an output under .test-artifacts")
    worker = args.worker.resolve(strict=True)
    root.mkdir(parents=True, exist_ok=False)
    run_matrix(root, worker, args.baseline.resolve(strict=True), compare_supervisors=args.compare_supervisors)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
