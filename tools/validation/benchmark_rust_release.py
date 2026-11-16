"""Compare installed Python, the tagged Rust build and a Rust candidate."""

import argparse
import json
from pathlib import Path
from statistics import median
import subprocess
from time import perf_counter
from unittest.mock import patch

from tools.validation.verify_large_input_rss import measure
from tools.validation.verify_rust_canonical_routes import output_model
from tools.validation.verify_rust_full_parity import digest, native_environment


def workload(fixtures):
    def paths(*extensions):
        return [Path(fixtures[ext]["path"]) for ext in extensions]

    return [
        ("copy", "tool:copy_original", paths("png", "mp4", "pdf", "docx", "xlsx", "zip")),
        ("image", "image:png", paths("jpg", "svg", "webp", "heic", "avif")),
        ("heif", "image:avif", paths("svg", "png", "heic")),
        ("media", "media:webm", paths("mp4", "wav")),
        ("audio", "audio:flac", paths("wav", "mp3")),
        ("media-tool", "tool:media_remux_mkv", paths("mp4", "mkv")),
        ("pdf", "pptx_editable", paths("pdf")),
        ("word", "document:hwpx", paths("docx", "xlsx", "pptx")),
        ("hwp", "document:pdf", paths("hwp", "hwpx")),
        ("office", "document:pdf", paths("docx", "xlsx", "pptx")),
        ("ebook", "document:epub", paths("epub", "md")),
        ("sheets", "sheet:csv", paths("xlsx", "ods")),
        ("archives", "archive:zip", paths("tar", "gz")),
        ("subtitles", "subtitle:vtt", paths("srt", "vtt")),
        ("data", "data:json", paths("csv", "tsv", "yaml", "toml")),
        ("font", "font:woff2", paths("ttf", "otf")),
        ("mesh", "mesh:stl", paths("obj", "ply")),
        ("geo", "geo:gpx", paths("kml", "geojson")),
        ("email", "document:txt", paths("eml")),
    ]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ("reference", "python", "original", "candidate", "output"):
        parser.add_argument(f"--{name}", type=Path, required=True)
    parser.add_argument("--workbook", type=Path, help="Optional large, mixed-script workbook.")
    parser.add_argument("--matrix", type=Path, help="Measure the complete route planner separately.")
    parser.add_argument("--only", nargs="+", help="Run the named workloads.")
    parser.add_argument("--ffmpeg", type=Path, help="Use one FFmpeg build for all three editions.")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    binaries = {name: getattr(args, name).resolve(strict=True)
                for name in ("python", "original", "candidate")}
    prior = json.loads(args.reference.read_bytes())
    cases = prior["cases"]
    if prior.get("status") != "complete":
        parser.error("Use a completed route comparison.")
    selections = {Path(item["path"]): ext for ext, item in prior["fixtures"].items()}
    report = {"status": "running", "sha256": {k: digest(v) for k, v in binaries.items()},
              "warmup_rounds": 1, "measured_rounds": 3, "cases": {}}
    report_path = root / "report.json"
    environment = native_environment()
    if args.ffmpeg:
        ffmpeg = args.ffmpeg.resolve(strict=True)
        report["ffmpeg_sha256"] = digest(ffmpeg)
        report["ffprobe_sha256"] = digest(ffmpeg.with_name("ffprobe.exe"))
        environment["MAGIC_CONVERTER_FFMPEG"] = str(ffmpeg)

    def save():
        report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")

    try:
        with patch.dict("os.environ", environment, clear=True):
            if args.matrix:
                expected = None
                planning = report["planning"] = {"samples_s": {"original": [], "candidate": []}}
                matrix = args.matrix.read_bytes()
                for iteration in range(8):
                    editions = ("original", "candidate")
                    for edition in editions if iteration % 2 == 0 else editions[::-1]:
                        started = perf_counter()
                        result = subprocess.run([binaries[edition], "compare-plans"], input=matrix,
                                                capture_output=True, check=True, timeout=60)
                        seconds = perf_counter() - started
                        if expected is None:
                            expected = result.stdout
                        elif result.stdout != expected:
                            raise ValueError("Candidate route planner changed its output")
                        if iteration:
                            planning["samples_s"][edition].append(seconds)
                planning["queries"] = len(matrix.splitlines())
                planning["byte_identical"] = True
                planning["median_s"] = {edition: median(samples)
                                        for edition, samples in planning["samples_s"].items()}
                save()
            work = workload(prior["fixtures"])
            work.append(("populated-history", "tool:copy_original", [Path(prior["fixtures"]["txt"]["path"])]))
            if args.workbook:
                work.append(("large-workbook", "document:pdf", [args.workbook.resolve(strict=True)]))
            if args.only:
                work = [case for case in work if case[0] in args.only]
                if {case[0] for case in work} != set(args.only):
                    parser.error("Unknown workload in --only")
            for name, action, sources in work:
                if name != "large-workbook":
                    sources = [path for path in sources if cases.get(f"{selections[path]}/{action}", {}).get("same_state")
                               and cases[f"{selections[path]}/{action}"]["python"]["state"] == "done"]
                if not sources:
                    raise ValueError(f"No baseline successes for {name}: {action}")
                print(name, action, len(sources), flush=True)
                expected = None
                fingerprints = {str(source): output_model(source) for source in sources}
                row = report["cases"][name] = {
                    "action": action, "inputs": fingerprints, "runs": [],
                    "measured_rounds_by_edition": {edition: 1 if name == "large-workbook" and edition == "python"
                                                   else 3 for edition in binaries},
                }
                order = tuple(binaries)
                for round_number in range(4):
                    editions = order if round_number % 2 == 0 else order[::-1]
                    if name == "large-workbook" and round_number != 1:
                        editions = tuple(edition for edition in editions if edition != "python")
                    for edition in editions:
                        folder = root / name / f"{round_number}-{edition}"
                        output = folder / "output"
                        output.mkdir(parents=True)
                        profile = folder / "profile"
                        if name == "populated-history":
                            profile.mkdir()
                            entries = [
                                {"id": f"bench-{index}", "action_key": action,
                                 "started_at": "", "finished_at": "", "label": "Copy", "preset_id": "",
                                 "options": {}, "output": {},
                                 "files": [{"source": str(source), "state": "done", "outputs": [str(source)],
                                            "warnings": ["기록 크기를 맞추기 위한 변환 경고" * 18], "errors": []}
                                           for source in sources * 20]}
                                for index in range(100)
                            ]
                            history = profile / "history.json"
                            history.write_text(json.dumps({"version": 1, "entries": entries}, ensure_ascii=False),
                                               encoding="utf-8")
                        measured = measure(
                            [str(binaries[edition]), "convert", "--action", action, "--output", str(output),
                             "--data-dir", str(profile), "--timeout", "600", "--json", *map(str, sources)],
                            folder / "measure", expected_ok=True, deadline=660,
                            inspect_outputs=lambda paths: {path.name: output_model(path) for path in paths},
                        )
                        model = measured["outputs"]
                        if len(model) != len(sources):
                            raise ValueError(f"{name} {edition}: wrong output count")
                        if edition == "original" and round_number == 0:
                            expected = model
                        if edition != "python" and expected.keys() != model.keys():
                            raise ValueError(f"{name} {edition}: output names changed")
                        if name == "populated-history":
                            recorded = json.loads(history.read_bytes())["entries"]
                            if len(recorded) != len(entries) + 1 or recorded[:-1] != entries:
                                raise ValueError(f"{edition}: conversion changed existing history")
                        clock = measured["clock_ns"]
                        row["runs"].append({
                            "edition": edition, "warmup": round_number == 0,
                            "seconds": (clock["tree_exit_observed"] - clock["launch"]) / 1e9,
                            "rss_bytes": measured["sampled_tree_peak_rss_bytes"],
                            "commit_bytes": measured["job_peak_commit_bytes"],
                            "io": measured["job_accounting"]["IoInfo"],
                            "matches_original_bytes": model == expected if edition != "python" else None,
                        })
                    save()
                row["median"] = {
                    edition: {metric: median(run[metric] for run in row["runs"]
                                             if run["edition"] == edition and not run["warmup"])
                              for metric in ("seconds", "rss_bytes", "commit_bytes")}
                    for edition in binaries
                }
                if fingerprints != {str(source): output_model(source) for source in sources}:
                    raise ValueError(f"{name}: source changed")
                save()
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=str(error))
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
