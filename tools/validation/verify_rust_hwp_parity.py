"""Compare the standalone HWPX/HWP routes with the pinned Python implementation."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import olefile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run
from tools.validation.verify_rust_office_render_parity import compare_pdf


def digest_bytes(value):
    return hashlib.sha256(value).hexdigest()


def hwpx_model(path):
    with ZipFile(path) as package:
        return [(name, digest_bytes(package.read(name))) for name in sorted(package.namelist())
                if name.startswith("BinData/") or name.startswith("Contents/section")]


def hwp_model(path):
    with olefile.OleFileIO(path) as package:
        return [(name, digest_bytes(package.openstream(name).read())) for name in sorted(package.listdir())
                if name[0] in {"BodyText", "BinData"}]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--rhwp", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    os.environ["MAGIC_CONVERTER_RHWP"] = str(args.rhwp.resolve())
    inputs = [ROOT / "tests/fixtures/hwp" / name for name in ("table-picture-spacing.hwpx", "script-faces.hwpx")]
    hwp = root / "from-hwpx.hwp"
    subprocess.run([args.rhwp.resolve(), "convert", inputs[0], hwp], check=True, capture_output=True, timeout=30)
    sources = [*inputs, hwp]
    evidence = {"worker_sha256": digest(args.worker), "cases": {}}
    try:
        for source in sources:
            for action in ("txt", "pdf", "hwp" if source.suffix == ".hwpx" else "hwpx"):
                key = f"{source.name}-{action}"
                print(key, flush=True)
                check = {"equal": False}
                evidence["cases"][key] = check
                receipts = []
                for backend, command, environment in (
                    ("python", [sys.executable, "-B", args.baseline.resolve() / "run_magic_converter.py"], None),
                    ("rust", [args.worker.resolve()], native_environment()),
                ):
                    path = root / key / backend
                    path.mkdir(parents=True)
                    code, receipt = run(command, ("convert", "--action", f"document:{action}", "--timeout", 60,
                                                  "--output", path, "--data-dir", path.parent / f"{backend}-profile", source),
                                        environment=environment)
                    check[backend] = {"code": code, "receipt": receipt}
                    if code == 0:
                        receipts.append(Path(receipt["jobs"][0]["outputs"][0]))
                if len(receipts) != 2:
                    continue
                if action == "pdf":
                    check.update(compare_pdf(*receipts))
                elif action == "txt":
                    check["equal"] = receipts[0].read_bytes() == receipts[1].read_bytes()
                else:
                    compare = hwp_model if action == "hwp" else hwpx_model
                    check["equal"] = compare(receipts[0]) == compare(receipts[1])
    finally:
        evidence["equal"] = sum(row["equal"] for row in evidence["cases"].values())
        evidence["total"] = len(evidence["cases"])
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")
    if evidence["equal"] != evidence["total"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
