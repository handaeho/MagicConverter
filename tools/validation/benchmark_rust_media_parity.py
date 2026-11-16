"""Compare full CLI media runs using the same FFmpeg and FFprobe binaries."""

import argparse
import json
import os
from pathlib import Path
import statistics
import sys
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_large_input_rss import measure
from tools.validation.verify_rust_full_parity import digest
from tools.validation.verify_rust_media_parity import AUDIO, VIDEO, equivalent, ffmpeg, fixtures

SEQUENCE = ("python", "rust", "rust", "python") * 2


def cases(root, engine):
    root.mkdir()
    audio = root / "PCM24.wav"
    ffmpeg(engine, "-f", "lavfi", "-i", "sine=frequency=740:sample_rate=48000:duration=120",
           "-ac", "2", "-c:a", "pcm_s24le", audio)
    video = root / "720p.mp4"
    ffmpeg(engine, "-f", "lavfi", "-i", "testsrc2=size=1280x720:rate=24:duration=8",
           "-f", "lavfi", "-i", "sine=frequency=740:sample_rate=48000:duration=8",
           "-c:v", "libx264", "-preset", "veryfast", "-c:a", "aac", "-threads", "2", video)
    small = fixtures(root / "short", engine)
    return {
        "wav-to-flac": ((audio,), "audio:flac"),
        "mp4-to-webm": ((video,), "media:webm"),
        "mp4-to-mkv-remux": ((video,), "tool:media_remux_mkv"),
        "short-media-batch-to-mp4": (tuple(small.values()), "media:mp4"),
    }


def main():
    parser = argparse.ArgumentParser()
    reference = parser.add_mutually_exclusive_group(required=True)
    reference.add_argument("--baseline", type=Path)
    reference.add_argument("--python-cli", type=Path)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--ffmpeg", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    engine = args.ffmpeg.resolve(strict=True)
    probe = engine.with_name("ffprobe.exe")
    python = ([str(args.python_cli.resolve(strict=True))] if args.python_cli else
              [sys.executable, "-B", str(args.baseline.resolve(strict=True) / "run_magic_converter.py")])
    commands = {"python": python,
                "rust": [str(args.worker.resolve(strict=True))]}
    workloads = cases(root / "inputs", engine)
    fingerprints = {path: digest(path) for paths, _ in workloads.values() for path in paths}
    report = {"status": "running", "sequence": SEQUENCE, "worker_sha256": digest(Path(commands["rust"][0])),
              "ffmpeg_sha256": digest(engine), "ffprobe_sha256": digest(probe), "runs": [], "summary": {}}
    if args.python_cli:
        report["python_cli_sha256"] = digest(Path(python[0]))

    def save():
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    save()
    try:
        for case, (sources, action) in workloads.items():
            references = []
            for index, edition in enumerate(("python", "rust", *SEQUENCE)):
                folder = root / case / f"{index:02d}-{edition}"
                output = folder / "output"
                output.mkdir(parents=True)
                cmd = [*commands[edition], "convert", *map(str, sources), "--action", action,
                       "--output", str(output), "--data-dir", str(folder / "profile"), "--timeout", "150", "--json"]
                with patch.dict(os.environ, {"MAGIC_CONVERTER_FFMPEG": str(engine), "PYTHONUTF8": "1"}):
                    measured = measure(cmd, folder / "measure", expected_ok=True, deadline=180)
                outputs = measured["outputs"]
                assert len(outputs) == len(sources)
                actual = [Path(item["path"]) for item in outputs]
                if index == 0:
                    references = actual
                comparisons = []
                for source, reference, result in zip(sources, references, actual, strict=True):
                    compare_still = (action.removeprefix("media:") in VIDEO
                                     and source.suffix.lower()[1:] in AUDIO)
                    comparisons.append(equivalent(
                        reference, result, engine, probe, compare_still=compare_still,
                        fixed_tail_source=source if compare_still and edition == "rust" else None,
                    ))
                if index > 0:
                    for path in actual:
                        path.unlink()
                times = measured["clock_ns"]
                report["runs"].append({
                    "case": case, "edition": edition, "warmup": index < 2,
                    "elapsed_s": (times["tree_exit_observed"] - times["launch"]) / 1e9,
                    "sampled_rss_bytes": measured["sampled_tree_peak_rss_bytes"],
                    "peak_job_commit_bytes": measured["job_peak_commit_bytes"],
                    "io": measured["job_accounting"]["IoInfo"],
                    "cpu_user_100ns": measured["job_accounting"]["BasicInfo"]["TotalUserTime"],
                    "results": comparisons, "process_count": measured["job_accounting"]["BasicInfo"]["TotalProcesses"],
                })
                save()
            report["summary"][case] = {}
            for edition in commands:
                rows = [row for row in report["runs"] if row["case"] == case and row["edition"] == edition and not row["warmup"]]
                report["summary"][case][edition] = {
                    metric: {"median": statistics.median(values), "min": min(values), "max": max(values)}
                    for metric in ("elapsed_s", "sampled_rss_bytes", "cpu_user_100ns", "process_count")
                    if (values := [row[metric] for row in rows])
                }
            print(case, flush=True)
        assert all(digest(path) == fingerprint for path, fingerprint in fingerprints.items())
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
