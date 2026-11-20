"""Exercise the Windows 3.0.5 conversion contract on the same synthetic files."""
import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import subprocess
import time
from zipfile import ZipFile


def invoke(worker, arguments, timeout=120, data=None):
    run = subprocess.run([str(worker), *map(str, arguments)], input=data, capture_output=True, timeout=timeout)
    return run.returncode, run.stdout, run.stderr


def output_model(path):
    if path.is_dir():
        return {"kind": "directory", "files": {item.relative_to(path).as_posix(): item.stat().st_size
                                                for item in sorted(path.rglob("*")) if item.is_file()}}
    data = path.read_bytes()
    if not data:
        raise AssertionError(f"Empty output: {path.name}")
    model = {"kind": "file", "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    if data.startswith(b"%PDF"):
        import pymupdf
        with pymupdf.open(path) as pdf:
            model.update(pages=len(pdf), text=" ".join(page.get_text() for page in pdf).split())
            if not len(pdf):
                raise AssertionError("The PDF has no pages")
    elif data.startswith(b"PK\x03\x04"):
        with ZipFile(path) as package:
            if package.testzip() is not None:
                raise AssertionError("Corrupt ZIP output")
            model["members"] = sorted(package.namelist())
    elif path.suffix.lower() in {".png", ".jpg", ".bmp", ".tiff", ".webp", ".gif", ".avif", ".heic"}:
        from PIL import Image
        with Image.open(path) as picture:
            picture.load()
            model.update(width=picture.width, height=picture.height, frames=getattr(picture, "n_frames", 1))
    return model


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, default=Path("tests/fixtures/macos/conversions.zip"))
    parser.add_argument("--contract", type=Path, default=Path("tests/fixtures/macos/windows-routes.json"))
    parser.add_argument("--reference", type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    fixtures = root / "inputs"
    with ZipFile(args.fixtures) as archive:
        archive.extractall(fixtures)
    for path in fixtures.rglob("*"):
        if path.is_file():
            os.utime(path, (1735689600, 1735689600))
    manifest = json.loads((fixtures / "manifest.json").read_text())
    contract = json.loads(args.contract.read_text())
    requests = b"".join((json.dumps(row["query"]) + "\n").encode() for row in contract)
    code, stdout, stderr = invoke(args.worker, ["compare-plans"], data=requests)
    if code:
        raise RuntimeError(stderr.decode(errors="replace"))
    actual = [json.loads(line) for line in stdout.splitlines()]
    if len(actual) != len(contract):
        raise AssertionError("Incomplete route comparison")
    missing = []
    cases = []
    for expected, result in zip(contract, actual, strict=True):
        if {key: result[key] for key in ("route", "reason")} != {key: expected["result"][key] for key in ("route", "reason")}:
            raise AssertionError(f"Route changed: {expected['query']}")
        if result["route"] and result["route"]["steps"]:
            if not result["native"]:
                missing.append(expected["query"])
            else:
                cases.append(expected["query"])
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except ImportError:
        pass

    def execute(item):
        extension = item["path"].removeprefix("input.")
        action = item["action"]
        name = extension.replace(".", "_") + "-" + action.replace(":", "_")
        folder = root / "cases" / name
        folder.mkdir(parents=True)
        started = time.monotonic()
        result = {"source": extension, "action": action}
        try:
            source = fixtures / manifest[extension]
            settings = folder / "profile"
            settings.mkdir()
            recipe = {"id": "comparison", "name": action, "action_key": action,
                      "tools": {}}
            if action == "tool:image_crop":
                recipe["tools"].update(crop_width=2, crop_height=2)
            if action in {"tool:media_trim", "tool:media_frames"}:
                recipe["tools"]["duration_ms"] = 500
            if action in {"tool:image_frames", "tool:media_frames"}:
                recipe["tools"]["max_frames"] = 100
            if action in {"tool:pdf_select", "tool:pdf_split", "tool:pdf_rotate"}:
                recipe["tools"]["pdf_pages"] = "1"
            (settings / "settings.json").write_text(json.dumps({"version": 3, "presets": [recipe]}))
            code, stdout, stderr = invoke(args.worker, ["convert", "--data-dir", settings, "--output", folder,
                                                       "--preset", "comparison", "--", source])
            report = json.loads(stdout)
            outputs = [Path(output) for job in report.get("jobs", []) for output in job.get("outputs", [])]
            result.update(ok=code == 0 and report.get("ok") is True and bool(outputs),
                          outputs=[output_model(output) for output in outputs],
                          errors=([report["error"]] if "error" in report else []) + [*report.get("errors", []), *(error for job in report.get("jobs", []) for error in job.get("errors", []))],
                          warnings=[warning for job in report.get("jobs", []) for warning in job.get("warnings", [])])
            if code and not result["errors"]:
                result["errors"] = [stderr.decode(errors="replace")[-2000:]]
        except Exception as error:
            result.update(ok=False, errors=[str(error)])
        result["seconds"] = round(time.monotonic() - started, 4)
        return result

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(execute, cases))
    failures = [result for result in results if not result["ok"]]
    regressions = failures
    if args.reference:
        reference = json.loads(args.reference.read_text(encoding="utf8"))
        expected = {(case["source"], case["action"]): case for case in reference["cases"]}
        regressions = [case for case in results if expected[(case["source"], case["action"])]["ok"] and not case["ok"]]
        if set(expected) != {(case["source"], case["action"]) for case in results}:
            raise AssertionError("The reference and candidate did not execute the same conversions")
    report = {"queries": len(contract), "routes": len(cases) + len(missing), "native": len(cases),
              "missing": missing, "executed": len(results), "passed": len(results) - len(failures),
              "failures": failures, "regressions": regressions, "cases": results}
    (root / "verification.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf8")
    print(json.dumps({key: value for key, value in report.items() if key != "cases"}, ensure_ascii=False, indent=2))
    raise SystemExit(bool(regressions or missing))


if __name__ == "__main__":
    main()
