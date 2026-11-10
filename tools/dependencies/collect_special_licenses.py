"""Refresh original license texts from the reviewed, installed stage-7 wheels."""

import hashlib
import json
from importlib.metadata import distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PACKAGES = ("fonttools", "Brotli", "zopfli", "PyYAML", "tomli-w", "defusedxml")


def main():
    output = ROOT / "licenses" / "stage7"
    output.mkdir(parents=True, exist_ok=True)
    manifest = []
    for name in PACKAGES:
        dist = distribution(name)
        count = 0
        for file in dist.files:
            if ".dist-info/" not in str(file) or Path(str(file)).name.lower() not in {
                "license",
                "license.external",
                "copying",
                "notice",
            }:
                continue
            data = Path(dist.locate_file(file)).read_bytes()
            destination = output / (name + "-" + Path(str(file)).name + ".txt")
            destination.write_bytes(data)
            manifest.append(
                {
                    "package": name,
                    "version": dist.version,
                    "file": destination.name,
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "upstream_installed_path": str(file),
                }
            )
            count += 1
        if not count:
            raise RuntimeError("No upstream license found: " + name)
    (output / "manifest.json").write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )


if __name__ == "__main__":
    main()
