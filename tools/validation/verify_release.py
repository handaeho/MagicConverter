"""Check the actual GUI/CLI PE resources, embedded manifest and license bytes."""

import argparse
import hashlib
import json
import sys
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter import __version__


def verify_sidecars(directory, archive):
    """External evidence must be valid JSON and byte-identical to each EXE."""
    names = {key.replace("\\", "/"): key for key in archive.toc}
    records = []
    for name in ("sbom.spdx.json", "build-inputs.json"):
        path = directory / name
        if not path.is_file() or path.is_symlink():
            raise ValueError("Missing/invalid release sidecar: " + name)
        data = path.read_bytes()
        json.loads(data)
        if data != archive.extract(names["build-metadata/" + name]):
            raise ValueError("Release sidecar differs from embedded evidence: " + name)
        records.append({"file": name, "bytes": len(data),
                        "sha256": hashlib.sha256(data).hexdigest()})
    return records


def verify(directory):
    import win32api
    import win32con
    from PyInstaller.archive.readers import CArchiveReader

    expected = tuple(map(int, __version__.split("."))) + (0,)
    licenses = json.loads(
        (ROOT / "licenses/stage7/manifest.json").read_text(encoding="utf-8")
    )
    records = []
    for name in ("magic_converter.exe", "magic_converter-cli.exe"):
        path = (directory / name).resolve()
        errors = []
        item = {"file": str(path), "errors": errors}
        try:
            info = win32api.GetFileVersionInfo(str(path), "\\")
            for key in ("FileVersion", "ProductVersion"):
                high, low = info[key + "MS"], info[key + "LS"]
                number = (high >> 16, high & 65535, low >> 16, low & 65535)
                item[key] = ".".join(map(str, number))
                if number != expected:
                    errors.append(key + " differs from package version")
            language, codepage = win32api.GetFileVersionInfo(
                str(path), "\\VarFileInfo\\Translation"
            )[0]
            prefix = f"\\StringFileInfo\\{language:04x}{codepage:04x}\\"
            strings = {
                key: win32api.GetFileVersionInfo(str(path), prefix + key)
                for key in (
                    "FileVersion",
                    "ProductVersion",
                    "OriginalFilename",
                    "InternalName",
                )
            }
            item["strings"] = strings
            if (
                strings["FileVersion"] != __version__
                or strings["ProductVersion"] != __version__
            ):
                errors.append("String versions differ from package version")
            if (
                strings["OriginalFilename"] != name
                or strings["InternalName"] != path.stem
            ):
                errors.append(
                    "OriginalFilename/InternalName do not identify this executable"
                )
            handle = win32api.LoadLibraryEx(
                str(path), 0, win32con.LOAD_LIBRARY_AS_DATAFILE
            )
            try:
                manifest = ET.fromstring(win32api.LoadResource(handle, 24, 1))
            finally:
                win32api.FreeLibrary(handle)
            identity = manifest.find(
                "{urn:schemas-microsoft-com:asm.v1}assemblyIdentity"
            )
            item["manifest_version"] = (
                identity.get("version") if identity is not None else None
            )
            if item["manifest_version"] != ".".join(map(str, expected)):
                errors.append("Embedded manifest version differs from package version")
            archive = CArchiveReader(str(path))
            from tools.build.build_inventory import verify_archive

            inventory = verify_archive(archive, ROOT)
            item["sidecars"] = verify_sidecars(directory, archive)
            frozen_inventory = directory / (path.stem + ".bundle.json")
            frozen_inventory.write_text(
                json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            item["sbom"] = {
                key: value for key, value in inventory.items() if key != "archive_files"
            }
            item["bundle_inventory"] = frozen_inventory.name
            item["bundle_inventory_sha256"] = hashlib.sha256(
                frozen_inventory.read_bytes()
            ).hexdigest()
            names = {key.replace("\\", "/"): key for key in archive.toc}
            checked = []
            for license in licenses:
                data = archive.extract(names["licenses/stage7/" + license["file"]])
                matched = hashlib.sha256(data).hexdigest() == license["sha256"]
                checked.append({"file": license["file"], "passed": matched})
                if not matched:
                    errors.append("Changed license bytes: " + license["file"])
            item["licenses"] = checked
            if (
                archive.extract(names["FORMAT_CONTRACTS.md"])
                != (ROOT / "docs/reference/FORMAT_CONTRACTS.md").read_bytes()
            ):
                errors.append("Bundled format contract differs from source")
            with path.open("rb") as stream:
                item["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
            item["bytes"] = path.stat().st_size
        except Exception as exc:
            errors.append(str(exc))
        item["status"] = "failed" if errors else "passed"
        records.append(item)
    return {
        "status": (
            "passed" if all(r["status"] == "passed" for r in records) else "failed"
        ),
        "expected_version": __version__,
        "artifacts": records,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "directory", nargs="?", type=Path, default=ROOT / "dist" / ("v" + __version__)
    )
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    result = verify(args.directory)
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
