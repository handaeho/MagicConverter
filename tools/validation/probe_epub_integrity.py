"""Additional EPUB audit cases; findings are reported, not silently repaired."""

import argparse
import base64
import io
import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Fail on any invalid EPUB or refusal of valid controls",
    )
    parser.add_argument(
        "--executable",
        type=Path,
        help="Use the console EXE with an isolated profile instead of source imports",
    )
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    from magic_converter.core.conversion.builtin_converters import create_catalog
    from magic_converter.core.conversion.conversion_request import prepare_request
    from magic_converter.core.conversion.conversion_execution import execute_file
    from magic_converter.core.conversion.converter import ConversionError

    raw = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(raw, "BMP")
    cases = {
        "mime_mismatch.md": '<p><img src="data:image/png;base64,'
        + base64.b64encode(raw.getvalue()).decode("ascii")
        + '"/></p>',
        "duplicate_ids.md": '<p id="same">First</p><p id="same">Second</p>',
        "fragment_control.md": '<p id="one">First <a href="#one">self</a></p>',
        "fb2_notes.fb2": '<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink"><description><title-info><genre>unrecognised</genre><author><nickname>Test Author</nickname></author><book-title>Notes</book-title><lang>en</lang></title-info><document-info><author><nickname>Test Author</nickname></author><date value="2026-10-22">2026-10-22</date><id>magic-notes-fixture</id><version>1.0</version></document-info></description><body><section><title><p>Main</p></title><p>Body <a type="note" l:href="#note1">[1]</a></p></section></body><body name="notes"><section id="note1"><title><p>Note</p></title><p>Note text</p></section></body></FictionBook>',
    }
    records = []
    for name, content in cases.items():
        source = args.output / name
        source.write_text(content, encoding="utf-8")
        notices = []
        record = {"case": name, "notices": notices}
        try:
            if args.executable:
                environment = os.environ.copy()
                environment["PATH"] = str(
                    Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32"
                )
                for key in (
                    "PYTHONPATH",
                    "PYTHONHOME",
                    "VIRTUAL_ENV",
                    "MAGIC_CONVERTER_FFMPEG",
                ):
                    environment.pop(key, None)
                result = subprocess.run(
                    [
                        str(args.executable.resolve()),
                        "convert",
                        str(source.resolve()),
                        "--to",
                        "epub",
                        "--output",
                        str((args.output / "converted").resolve()),
                        "--data-dir",
                        str((args.output / "profile").resolve()),
                        "--json",
                    ],
                    env=environment,
                    capture_output=True,
                    timeout=45,
                )
                response = json.loads(result.stdout.decode("utf-8-sig"))
                jobs = response.get("jobs")
                if not jobs:
                    raise RuntimeError(
                        "CLI returned no conversion job: " + str(response)
                    )
                notices.extend(jobs[0].get("warnings", []))
                if result.returncode:
                    raise ConversionError(str(jobs[0].get("errors")))
                output = Path(jobs[0]["outputs"][0])
            else:
                request = prepare_request([source], "document:epub", create_catalog())
                output = execute_file(
                    request, request.files[0], None, lambda _: None, notices.append
                )
        except ConversionError as exc:
            record.update(refused=True, error=str(exc), invalid_output=False)
            records.append(record)
            continue
        except Exception as exc:
            record.update(test_error=str(exc))
            records.append(record)
            continue
        try:
            report = args.output / (name + "-epubcheck.json")
            report.unlink(missing_ok=True)
            process = subprocess.run(
                [
                    "java",
                    "-jar",
                    str(args.jar.resolve()),
                    str(output.resolve()),
                    "--json",
                    str(report.resolve()),
                ],
                capture_output=True,
                timeout=40,
            )
            result = json.loads(report.read_text(encoding="utf-8"))
            if process.returncode not in (0, 1) or not result.get("checker"):
                raise RuntimeError("EPUBCheck did not finish a validation run")
            record.update(
                output=str(output.resolve()),
                exit_code=process.returncode,
                checker=result.get("checker"),
                messages=result.get("messages", []),
                invalid_output=process.returncode != 0,
            )
        except Exception as exc:
            record.update(test_error=str(exc))
        records.append(record)
        print(json.dumps(record, ensure_ascii=False))
    (args.output / "summary.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    control = next(r for r in records if r["case"] == "fragment_control.md")
    return (
        1
        if any(r.get("test_error") for r in records)
        or control.get("exit_code") != 0
        or (
            args.strict
            and (
                any(r.get("invalid_output") for r in records)
                or next(r for r in records if r["case"] == "fb2_notes.fb2").get(
                    "exit_code"
                )
                != 0
            )
        )
        else 0
    )


if __name__ == "__main__":
    raise SystemExit(main())
