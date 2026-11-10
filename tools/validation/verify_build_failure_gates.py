"""Inject broken frozen evidence without modifying the actual EXE."""

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.build.build_inventory import verify_archive
from PyInstaller.archive.readers import CArchiveReader


class InjectedArchive:
    def __init__(self, archive, name, transform):
        self.archive, self.name, self.transform = archive, name, transform
        self.toc = archive.toc

    def extract(self, name):
        data = self.archive.extract(name)
        return self.transform(data) if name.replace("\\", "/") == self.name else data

    def open_embedded_archive(self, name):
        return self.archive.open_embedded_archive(name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    archive = CArchiveReader(str(args.executable))
    positive = verify_archive(archive)

    def bad_schema(data):
        value = json.loads(data)
        value.pop("creationInfo")
        return json.dumps(value).encode()

    def bad_coverage(data):
        value = json.loads(data)
        next(f for f in value["files"] if f["group"] == "binary")[
            "target"
        ] = "unlisted-binary.dll"
        return json.dumps(value).encode()

    cases = [
        ("missing SPDX required field", "build-metadata/sbom.spdx.json", bad_schema),
        ("changed input coverage", "build-metadata/build-inputs.json", bad_coverage),
        (
            "changed original notice",
            "licenses/runtime/python-3.14.7/LICENSE.txt",
            lambda _: b"tampered",
        ),
    ]
    names = {n.replace("\\", "/"): n for n in archive.toc}
    payload = json.loads(archive.extract(names["build-metadata/build-inputs.json"]))

    def flip(data):
        altered = bytearray(data)
        altered[len(altered) // 2] ^= 1
        return bytes(altered)

    for owner in ("pymupdf", "pyside6-essentials", "python", "windows", "vendor-0"):
        candidates = [f for f in payload["files"] if f["group"] == "binary" and f["owner"] == owner]
        if candidates:
            cases.append(("one-byte native change: " + owner, candidates[0]["target"], flip))

    def altered_claim(field, value):
        def transform(data):
            document = json.loads(data)
            next(p for p in document["packages"] if p["name"] == "pymupdf")[field] = value
            return json.dumps(document).encode()
        return transform

    for field, value in (("versionInfo", "0.0.0-audit"), ("licenseDeclared", "MIT"),
                         ("licenseConcluded", "MIT"), ("externalRefs", []),
                         ("checksums", [{"algorithm": "SHA256", "checksumValue": "0" * 64}])):
        cases.append(("changed package " + field, "build-metadata/sbom.spdx.json", altered_claim(field, value)))
    results = []
    for name, target, transform in cases:
        try:
            verify_archive(InjectedArchive(archive, target, transform))
        except Exception as exc:
            results.append({"case": name, "rejected": True, "error": str(exc)[:600]})
        else:
            results.append({"case": name, "rejected": False})
    from tools.validation.verify_release import verify_sidecars

    with tempfile.TemporaryDirectory(prefix="release-sidecars-") as temp:
        directory = Path(temp)
        originals = {name: archive.extract(names["build-metadata/" + name]) for name in ("sbom.spdx.json", "build-inputs.json")}
        for name, data in originals.items():
            (directory / name).write_bytes(data)
        sidecar_control = verify_sidecars(directory, archive)
        for name, original in originals.items():
            for label, altered in (("missing", None), ("invalid JSON", b"invalid"), ("changed", b"{}")):
                path = directory / name
                if altered is None:
                    path.unlink()
                else:
                    path.write_bytes(altered)
                try:
                    verify_sidecars(directory, archive)
                except Exception as exc:
                    results.append({"case": label + " sidecar " + name, "rejected": True, "error": str(exc)[:600]})
                else:
                    results.append({"case": label + " sidecar " + name, "rejected": False})
                path.write_bytes(original)
    result = {
        "status": "passed" if all(r["rejected"] for r in results) else "failed",
        "positive_archive_files": len(positive["archive_files"]),
        "positive_sidecars": sidecar_control,
        "cases": results,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
