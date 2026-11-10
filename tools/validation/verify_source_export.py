"""A09: compare actual Git-exported original bytes with the reviewed hashes."""

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_license_checkout import reviewed_files


def verify(root=ROOT):
    artifact_root = root / ".test-artifacts"
    artifact_root.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(dir=artifact_root, prefix="git-export-") as temp:
        archive_path = Path(temp) / "source.zip"
        subprocess.run(["git", "archive", "--format=zip", "--output", str(archive_path), "HEAD"], cwd=root, check=True)
        records = []
        with zipfile.ZipFile(archive_path) as archive:
            for entry in reviewed_files(root):
                try:
                    actual = hashlib.sha256(archive.read(entry["file"])).hexdigest()
                except KeyError:
                    actual = None
                records.append({"file": entry["file"], "sha256": actual, "passed": actual == entry["sha256"]})
    return {"status": "passed" if all(r["passed"] for r in records) else "failed", "files": records}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = verify()
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps({"status": result["status"], "checked": len(result["files"])}))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
