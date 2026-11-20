"""Verify offline legacy presentation and workbook print output without installed Office."""
import argparse
import json
from pathlib import Path
import subprocess
from zipfile import ZipFile

import pymupdf


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--office", type=Path, required=True)
    parser.add_argument("--worker", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    inputs = root / "inputs"
    with ZipFile("tests/fixtures/macos/conversions.zip") as archive:
        archive.extractall(inputs)
    manifest = json.loads((inputs / "manifest.json").read_text())
    records = {}

    def export(source, extension, name):
        destination = root / name
        destination.mkdir()
        profile = (root / (name + "-profile")).as_uri()
        run = subprocess.run([args.office, "-env:UserInstallation=" + profile, "--headless", "--convert-to",
                              extension, "--outdir", destination, source], capture_output=True, text=True, timeout=120)
        output = destination / (source.stem + "." + extension)
        assert run.returncode == 0 and output.is_file(), (run.stdout, run.stderr)
        return output

    book = export(inputs / manifest["xlsx"], "pdf", "workbook")
    with pymupdf.open(book) as document:
        assert len(document) and "".join(page.get_text() for page in document).strip()
        records["workbook_print_pages"] = len(document)
    presentation = inputs / manifest["pptx"]
    legacy = export(presentation, "ppt", "legacy")
    printed = export(legacy, "pdf", "presentation")
    with pymupdf.open(printed) as document:
        assert len(document) and "".join(page.get_text() for page in document).strip()
        records["legacy_ppt_pages"] = len(document)
    if args.worker:
        for source, action in [(inputs / manifest["xlsx"], "office:xlsx_print_pdf"),
                               *[(legacy, "document:" + suffix) for suffix in ("pdf", "jpg", "png")]]:
            destination = root / action.replace(":", "-")
            result = subprocess.run([args.worker, "convert", "--action", action, "--data-dir", root / "profile",
                                     "--output", destination, "--", source], capture_output=True, timeout=120)
            report = json.loads(result.stdout)
            assert result.returncode == 0 and report["ok"], report
            records[action] = True
    records["status"] = "passed"
    (root / "report.json").write_text(json.dumps(records, indent=2) + "\n")
    print(json.dumps(records))


if __name__ == "__main__":
    main()
