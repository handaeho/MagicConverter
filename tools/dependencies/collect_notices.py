"""Collect exact original notices for the committed wheel set; explicit refresh only."""

import hashlib
import importlib.metadata as metadata
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def license_file(path):
    if "__pycache__" in Path(path).parts or Path(path).suffix.lower() in {
        ".py",
        ".pyc",
        ".pyo",
        ".pyi",
        ".pyd",
        ".typed",
    }:
        return False
    value = str(path).lower()
    return any(
        word in value
        for word in ("license", "licence", "copying", "copyright", "notice")
    )


def main():
    manifest_path = ROOT / "packaging/notices-lock.json"
    previous = (
        json.loads(manifest_path.read_text(encoding="utf-8"))
        if manifest_path.is_file()
        else {"packages": []}
    )
    wheels = json.loads(
        (ROOT / "packaging/python-wheels.json").read_text(encoding="utf-8")
    )
    records = []
    for wheel in wheels["wheels"]:
        dist = metadata.distribution(wheel["name"])
        if dist.version != wheel["version"]:
            raise RuntimeError(
                "Installed package differs from reviewed wheel: " + wheel["name"]
            )
        prefix = Path("licenses/runtime") / (wheel["name"] + "-" + dist.version)
        files = []
        for member in dist.files or ():
            if not license_file(member):
                continue
            source = Path(dist.locate_file(member))
            if not source.is_file():
                raise RuntimeError("Original package notice is missing: " + str(member))
            relative = Path(str(member).replace("..", "_parent"))
            destination = ROOT / prefix / relative
            if not destination.resolve().is_relative_to(
                (ROOT / "licenses/runtime").resolve()
            ):
                raise RuntimeError("Notice path escapes collection")
            data = source.read_bytes()
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)
            files.append(
                {
                    "file": destination.relative_to(ROOT).as_posix(),
                    "source": str(member),
                    "sha256": hashlib.sha256(data).hexdigest(),
                    "bytes": len(data),
                }
            )
        if not files:
            raise RuntimeError("No full original notice found: " + wheel["name"])
        raw_metadata = dist.read_text("METADATA")
        destination = ROOT / prefix / "METADATA.txt"
        destination.write_text(raw_metadata, encoding="utf-8", newline="")
        records.append(
            {
                "name": wheel["name"],
                "version": dist.version,
                "declared_expression": dist.metadata.get("License-Expression"),
                "declared_license": dist.metadata.get("License"),
                "project_urls": dist.metadata.get_all("Project-URL") or [],
                "home_page": dist.metadata.get("Home-page"),
                "notices": files,
                "metadata": destination.relative_to(ROOT).as_posix(),
                "metadata_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
            }
        )
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    output = ROOT / "licenses/runtime" / ("python-" + wheels["python"]) / "LICENSE.txt"
    output.parent.mkdir(exist_ok=True)
    output.write_bytes(python_license.read_bytes())
    manifest = {
        "schema_version": 1,
        "python": {
            "version": wheels["python"],
            "notice": output.relative_to(ROOT).as_posix(),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
        },
        "packages": records,
    }
    retained = {n["file"] for p in records for n in p["notices"]}
    for package in previous["packages"]:
        for entry in package["notices"]:
            if entry["file"] in retained:
                continue
            obsolete = (ROOT / entry["file"]).resolve()
            if not obsolete.is_relative_to((ROOT / "licenses/runtime").resolve()):
                raise ValueError("Obsolete notice path escapes collection")
            if obsolete.is_file():
                if hashlib.sha256(obsolete.read_bytes()).hexdigest() != entry["sha256"]:
                    raise ValueError("Obsolete notice was modified; refusing removal")
                obsolete.unlink()
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        "Collected",
        len(records),
        "package notice sets /",
        sum(len(r["notices"]) for r in records),
        "original files, plus Python",
    )


if __name__ == "__main__":
    main()
