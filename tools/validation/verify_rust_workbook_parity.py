"""Measure and audit every cell of the pinned workbook through both CLI builds."""

import argparse
import json
import os
from pathlib import Path
import statistics
import sys

from openpyxl import Workbook

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_large_input_rss import measure
from tools.validation.verify_large_workbook import pdf_contents
from tools.validation.verify_rust_full_parity import digest
from tools.validation.verify_rust_image_parity import pixel_metrics

SOURCE = "b94f6f40b91d3dd496416e0450be92e37271f79519e69168946706bfdc30b1ff"
CELLS = "695c8e96ea112619ed10c8437133cb1034a8c55e0739db0ad0a6c31726497531"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    source = args.input.resolve()
    assert digest(source) == SOURCE
    warmup = root / "warmup.xlsx"
    book = Workbook()
    sheet = book.active
    sheet.title = "한글 표"
    sheet.append(["Warmup", "한글", "127.2", 1])
    sheet["B100000"] = "=1+1"
    book.save(warmup)
    book.close()
    evidence = {"status": "running", "source_sha256": SOURCE, "worker_sha256": digest(args.worker), "runs": []}
    sequence = [("python", warmup), ("rust", warmup)]
    if not args.smoke:
        sequence.extend((name, source) for name in ("python", "rust", "rust", "python", "python", "rust", "rust", "python"))
    python = [sys.executable, "-B", str(args.baseline.resolve() / "run_magic_converter.py")]
    rust = [str(args.worker.resolve())]
    staged = []

    def save():
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")

    save()
    try:
        for index, (backend, workbook) in enumerate(sequence):
            path = root / f"{index:02d}-{backend}"
            path.mkdir()
            print(f"{index + 1}/{len(sequence)} {backend} {workbook.name}", flush=True)
            command = (python if backend == "python" else rust) + [
                "convert", str(workbook), "--action", "document:pdf", "--output", str(path / "output"),
                "--data-dir", str(path / "profile"), "--timeout", "900", "--json",
            ]
            old_path = os.environ["PATH"]
            if backend == "rust":
                os.environ["PATH"] = str(Path(os.environ.get("SYSTEMROOT", "C:/Windows")) / "System32")
            try:
                measurement = measure(command, path / "measure", expected_ok=True, deadline=960)
            finally:
                os.environ["PATH"] = old_path
            output, = measurement["outputs"]
            record = {"backend": backend, "warmup": workbook == warmup, "measurement": measurement}
            evidence["runs"].append(record)
            staged.append((record, Path(output["path"]), path))
            save()

        first = {}
        from PIL import Image
        for index, (record, pdf, folder) in enumerate(staged):
            print(f"Auditing {index + 1}/{len(staged)} {record['backend']}", flush=True)
            record["audit"] = pdf_contents(pdf, folder)
            if not record["warmup"]:
                assert (record["audit"]["pages"], record["audit"]["cells"], record["audit"]["content_sha256"]) == (29448, 8681985, CELLS)
            rendered = [Image.open(path).convert("RGB").tobytes() for path in sorted(folder.glob("page-*.png"))]
            key = record["warmup"]
            if key in first:
                record["render_comparison"] = pixel_metrics(first[key], rendered)
            else:
                first[key] = rendered
            save()
        warm = [record["audit"] for record in evidence["runs"] if record["warmup"]]
        assert warm[0]["content_sha256"] == warm[1]["content_sha256"]
        assert digest(source) == SOURCE
        if not args.smoke:
            evidence["median_seconds"] = {name: statistics.median(
                record["measurement"]["seconds"] for record in evidence["runs"]
                if record["backend"] == name and not record["warmup"]) for name in ("python", "rust")}
        evidence["status"] = "passed"
    except BaseException as error:
        evidence["status"] = "failed"
        evidence["error"] = repr(error)
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
