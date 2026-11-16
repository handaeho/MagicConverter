"""Execute every canonical plan against a fixture and compare CLI receipts."""

import argparse
from collections import defaultdict
import json
from pathlib import Path
import shutil
import subprocess
import sys
from time import perf_counter

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run
from tools.validation.verify_rust_office_render_parity import compare_pdf


def plans(matrix):
    with matrix.open(encoding="utf-8") as stream:
        for line in stream:
            row = json.loads(line)
            if row["profile"] == "all" and row["path"].startswith("input.") and row["route"] and row["route"]["steps"]:
                yield row["path"].removeprefix("input."), row["action"], row["route"]["steps"]


def identity(path):
    return (str(path.resolve()).casefold(), path.name.casefold())


def output_model(path):
    if path.is_dir():
        files = sorted(file for file in path.rglob("*") if file.is_file())
        return {"kind": "folder", "files": {file.relative_to(path).as_posix(): digest(file) for file in files}}
    return {"kind": "file", "bytes": path.stat().st_size, "sha256": digest(path)}


def summarize(receipt, source, force_all=False):
    jobs = receipt.get("jobs", [])
    if force_all:
        return {
            "state": "done" if receipt.get("ok") else "failed",
            "outputs": [value for job in jobs for value in job.get("outputs", [])],
            "warnings": [value for job in jobs for value in job.get("warnings", [])],
            "errors": [*receipt.get("errors", []), *(value for job in jobs for value in job.get("errors", []))],
        }
    for job in jobs:
        for file in job.get("files", []):
            if identity(Path(file["source"])) == identity(source):
                return {field: file.get(field, []) for field in ("state", "outputs", "warnings", "errors")}
    return {"state": "unreported", "outputs": [], "warnings": [],
            "errors": [*receipt.get("errors", []), *(value for job in jobs for value in job.get("errors", []))]}


def main():
    parser = argparse.ArgumentParser()
    python = parser.add_mutually_exclusive_group(required=True)
    python.add_argument("--baseline", type=Path)
    python.add_argument("--python-cli", type=Path, help="Measure the installed Python CLI directly.")
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    parser.add_argument("--fixtures", type=Path, required=True, help="JSON mapping each canonical extension to a local fixture.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--resume", action="store_true", help="Continue an interrupted run with identical inputs and worker.")
    parser.add_argument("--reference-report", type=Path,
                        help="Reuse Python receipts and outputs from a completed run on identical fixtures.")
    args = parser.parse_args()
    root, worker = args.output.resolve(), args.worker.resolve()
    python_cli = args.python_cli.resolve(strict=True) if args.python_cli else None
    inputs = root / "inputs"
    inputs.mkdir(parents=True, exist_ok=args.resume)
    files = json.loads(args.fixtures.read_text(encoding="utf-8-sig"))
    inventory = {}
    grouped = defaultdict(list)
    rows = list(plans(args.matrix))
    reference = json.loads(args.reference_report.read_bytes()) if args.reference_report else None
    if reference and (reference.get("status") != "complete" or reference["total"] != len(rows)):
        raise ValueError("The reference report must cover every canonical route.")
    evidence = {"worker_sha256": digest(worker), "fixtures": inventory, "batches": {}, "cases": {}}
    if python_cli:
        evidence["python_cli_sha256"] = digest(python_cli)
    if reference:
        evidence["python_reference"] = str(args.reference_report.resolve())
    report = root / "report.json"
    if args.resume:
        previous = json.loads(report.read_bytes())
        routes = {f"{ext}/{action}": steps for ext, action, steps in rows}
        if (previous["worker_sha256"] != evidence["worker_sha256"] or previous["total"] != len(rows)
                or previous.get("python_cli_sha256") != evidence.get("python_cli_sha256")
                or previous.get("python_reference") != evidence.get("python_reference")
                or any(routes.get(key) != case["steps"] for key, case in previous["cases"].items())):
            raise ValueError("The worker or routes changed since the earlier run.")
        evidence["batches"] = previous["batches"]
        evidence["cases"] = previous["cases"]
    for extension in dict.fromkeys(ext for ext, _, _ in rows):
        if extension not in files:
            raise ValueError(f"Missing canonical fixture: {extension}")
        source = Path(files[extension]).resolve()
        target = inputs / f"원본-{extension.replace('.', '-')}.{extension}"
        if args.resume:
            if output_model(source) != output_model(target):
                raise ValueError(f"Fixture changed while resuming: {extension}")
        elif source.is_dir():
            shutil.copytree(source, target)
        else:
            shutil.copyfile(source, target)
        inventory[extension] = {"path": str(target), **output_model(target)}
        if reference and {k: v for k, v in inventory[extension].items() if k != "path"} != {
            k: v for k, v in reference["fixtures"][extension].items() if k != "path"
        }:
            raise ValueError(f"Reference fixture changed: {extension}")
    extra = inputs / "merge-second.pdf"
    if any(action == "tool:pdf_merge" for _, action, _ in rows):
        shutil.copyfile(inventory["pdf"]["path"], extra)

    for ext, action, steps in rows:
        grouped[action].append((ext, Path(inventory[ext]["path"]), steps))
    commands = {
        "python": ([python_cli], native_environment()) if python_cli else
        ([sys.executable, "-B", args.baseline.resolve() / "run_magic_converter.py"], None),
        "rust": ([worker], native_environment()),
    }
    if reference:
        del commands["python"]

    def publish():
        results = list(evidence["cases"].values())
        evidence["total"] = len(rows)
        evidence["executed"] = len(results)
        evidence["both_completed"] = sum(x["python"]["state"] == x["rust"]["state"] == "done" for x in results)
        evidence["same_state"] = sum(x["same_state"] for x in results)
        evidence["same_names_and_bytes"] = sum(x["same_names_and_bytes"] for x in results)
        evidence["same_pdf_render"] = sum(x.get("pdf_equal", False) for x in results)
        report.write_text(json.dumps(evidence, indent=2, ensure_ascii=False), encoding="utf-8")

    def measure(key, action, entries, special=False):
        entries = [(ext, source, steps) for ext, source, steps in entries
                   if f"{ext}/{action}" not in evidence["cases"]]
        if not entries:
            return
        base = key
        retry = 1
        while (root / key).exists():
            key = f"{base}-retry-{retry}"
            retry += 1
        print(f"{key} {len(entries)}", flush=True)
        batch = evidence["batches"][key] = {}
        for backend, (command, environment) in commands.items():
            folder = root / key / backend
            folder.mkdir(parents=True)
            sources = [source for _, source, _ in entries]
            if action == "tool:pdf_merge":
                sources.append(extra)
            started = perf_counter()
            try:
                code, receipt = run(command, ("convert", "--action", action, "--timeout", 100, "--output", folder,
                                              "--data-dir", root / f"profile-{backend}", *sources),
                                    environment=environment)
            except (RuntimeError, subprocess.TimeoutExpired) as error:
                code, receipt = -1, {"ok": False, "errors": [str(error)], "jobs": []}
            batch[backend] = {"code": code, "seconds": perf_counter() - started, "receipt": receipt}
        for ext, source, steps in entries:
            case = {"action": action, "source": ext, "steps": steps}
            for backend in commands:
                case[backend] = summarize(batch[backend]["receipt"], source, special)
                paths = [Path(path) for path in case[backend]["outputs"]]
                case[backend]["model"] = {path.name: output_model(path) for path in paths}
            if reference:
                recorded = reference["cases"][f"{ext}/{action}"]
                if recorded["steps"] != steps:
                    raise ValueError(f"Reference route changed: {ext}/{action}")
                case["python"] = recorded["python"]
                paths = [Path(path) for path in case["python"]["outputs"]]
                if case["python"]["model"] != {path.name: output_model(path) for path in paths}:
                    raise ValueError(f"Reference output changed: {ext}/{action}")
            left, right = case["python"], case["rust"]
            if not special and "unreported" in (left["state"], right["state"]):
                measure(f"{key}-{ext.replace('.', '-')}", action, [(ext, source, steps)], True)
                continue
            case["same_state"] = left["state"] == right["state"] and left["state"] != "unreported"
            case["same_names_and_bytes"] = left["state"] == right["state"] == "done" and left["model"] == right["model"]
            if len(left["outputs"]) == len(right["outputs"]) == 1:
                a, b = (Path(item["outputs"][0]) for item in (left, right))
                if a.suffix.lower() == b.suffix.lower() == ".pdf" and a.is_file() and b.is_file():
                    case["pdf"] = compare_pdf(a, b)
                    case["pdf_equal"] = case["pdf"]["equal"]
            evidence["cases"][f"{ext}/{action}"] = case
        publish()

    publish()
    for action, entries in grouped.items():
        key = action.replace(":", "-")
        if action == "tool:pdf_merge":
            for ext, source, steps in entries:
                measure(f"{key}-{ext}", action, [(ext, source, steps)], True)
        elif action in {"document:pdf", "document:jpg", "document:png"}:
            for i in range(0, len(entries), 8):
                measure(f"{key}-{i // 8 + 1}", action, entries[i:i + 8])
        else:
            measure(key, action, entries)
    for ext, fixture in inventory.items():
        assert {k: v for k, v in fixture.items() if k != "path"} == output_model(Path(fixture["path"])), ext
    evidence["status"] = "complete"
    publish()


if __name__ == "__main__":
    main()
