"""Collect and verify the runtime bytes embedded in the Windows installer."""

import ast
import base64
import csv
import hashlib
import io
import json
import shutil
import tempfile
from copy import copy
from pathlib import Path, PurePosixPath
from urllib.request import urlopen
from zipfile import ZIP_STORED, ZipFile, ZipInfo


PYMUPDF_BUILD = "pymupdf/_build.py"
PYMUPDF_LOCAL_METADATA = {
    "pymupdf_git_branch": "",
    "pymupdf_git_comment": "",
    "pymupdf_git_diff": "",
    "pymupdf_git_diff_n": 0,
    "pymupdf_git_sha": "",
    "mupdf_location": "",
}


def pymupdf_build_assignments(archive):
    source = archive.read(PYMUPDF_BUILD).decode("utf-8")
    assignments = {}
    for statement in ast.parse(source).body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1:
            target = statement.targets[0]
            if isinstance(target, ast.Name) and target.id in PYMUPDF_LOCAL_METADATA:
                if target.id in assignments:
                    raise ValueError("Duplicate PyMuPDF build metadata assignment")
                assignments[target.id] = statement
    if assignments.keys() != PYMUPDF_LOCAL_METADATA.keys():
        raise ValueError("Unexpected PyMuPDF build metadata layout")
    return source, assignments


def verify_pymupdf_build_metadata(wheel):
    with ZipFile(wheel) as archive:
        _, assignments = pymupdf_build_assignments(archive)
        if any(ast.literal_eval(assignments[key].value) != value for key, value in PYMUPDF_LOCAL_METADATA.items()):
            raise ValueError("PyMuPDF wheel still contains local build metadata")


def clean_pymupdf_wheel(source, destination):
    """Remove captured build metadata and refresh RECORD while preserving the native binaries."""
    if destination.exists():
        raise FileExistsError(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(source) as wheel:
        names = wheel.namelist()
        if len(names) != len(set(names)) or any(name.endswith(("/RECORD.jws", "/RECORD.p7s")) for name in names):
            raise ValueError("Expected an unsigned PyMuPDF wheel with unique members")
        record_name, = (name for name in names if name.endswith(".dist-info/RECORD"))
        metadata, assignments = pymupdf_build_assignments(wheel)
        lines = metadata.splitlines(keepends=True)
        for key, statement in sorted(assignments.items(), key=lambda item: item[1].lineno, reverse=True):
            lines[statement.lineno - 1:statement.end_lineno] = [f"{key} = {PYMUPDF_LOCAL_METADATA[key]!r}\n"]
        cleaned = "".join(lines).encode("utf-8")
        rows = list(csv.reader(io.StringIO(wheel.read(record_name).decode("utf-8"))))
        record, = (row for row in rows if row[0] == PYMUPDF_BUILD)
        digest = base64.urlsafe_b64encode(hashlib.sha256(cleaned).digest()).rstrip(b"=").decode("ascii")
        record[1:] = ["sha256=" + digest, str(len(cleaned))]
        output = io.StringIO(newline="")
        csv.writer(output).writerows(rows)
        updates = {PYMUPDF_BUILD: cleaned, record_name: output.getvalue().encode("utf-8")}
        with tempfile.TemporaryDirectory(dir=destination.parent) as temporary:
            ready = Path(temporary) / destination.name
            with ZipFile(ready, "w") as clean:
                for entry in wheel.infolist():
                    data = updates[entry.filename] if entry.filename in updates else wheel.read(entry)
                    clean.writestr(copy(entry), data)
            verify_pymupdf_build_metadata(ready)
            ready.rename(destination)


def checked_file(path, digest):
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"Missing regular runtime file: {path.name}")
    with path.open("rb") as stream:
        if hashlib.file_digest(stream, "sha256").hexdigest() != digest:
            raise ValueError(f"Runtime SHA-256 mismatch: {path.name}")
    return path


def download(url, path):
    with urlopen(url, timeout=90) as response, path.open("xb") as output:
        shutil.copyfileobj(response, output)
    return path


def wheel_source(package, version, digest):
    with urlopen(f"https://pypi.org/pypi/{package}/{version}/json", timeout=45) as response:
        urls = json.load(response)["urls"]
    matches = [entry for entry in urls if entry["packagetype"] == "bdist_wheel"
               and entry["digests"]["sha256"] == digest]
    if len(matches) != 1:
        raise ValueError(f"PyPI does not have exactly one pinned wheel for {package} {version}")
    return matches[0]["filename"], matches[0]["url"]


def write_runtime_archive(destination, bootstrap, lock, runtime_dir=None):
    """Use cached publisher inputs or fetch the identical bytes at build time."""
    download_dir = destination.parent / "runtime-downloads"
    if runtime_dir is None:
        download_dir.mkdir()
        bundle = bootstrap.get("inputs_bundle")
        if bundle:
            package = checked_file(download(bundle["url"], download_dir / bundle["filename"]), bundle["sha256"])
            runtime_dir = download_dir / "inputs"
            with ZipFile(package) as archive:
                names = archive.namelist()
                if len({name.casefold() for name in names}) != len(names):
                    raise ValueError("Duplicate runtime input bundle members")
                for entry in archive.infolist():
                    path = PurePosixPath(entry.filename)
                    if (path.is_absolute() or ".." in path.parts or "\\" in entry.filename
                            or ":" in entry.filename or entry.external_attr >> 16 & 0o170000 == 0o120000):
                        raise ValueError("Unsafe runtime input bundle member: " + entry.filename)
                archive.extractall(runtime_dir)
    inputs = {}
    for kind in ("python", "pip"):
        pin = bootstrap[kind]
        name = pin["filename"]
        path = runtime_dir / name if runtime_dir is not None else download(pin["url"], download_dir / name)
        inputs[name] = (checked_file(path, pin["sha256"]), pin["sha256"])

    cached_wheels = tuple((runtime_dir / "wheels").glob("*.whl")) if runtime_dir is not None else ()
    for line in lock.decode("utf-8").splitlines()[1:]:
        requirement, pin = line.split()
        package, version = requirement.split("==")
        digest = pin.removeprefix("--hash=sha256:")
        if runtime_dir is None:
            name, url = wheel_source(package, version, digest)
            path = download(url, download_dir / name)
        else:
            prefix = f"{package.replace('-', '_')}-{version}-"
            matches = [path for path in cached_wheels if path.name.casefold().startswith(prefix.casefold())]
            if len(matches) != 1:
                raise ValueError(f"Expected one cached wheel for {requirement}")
            path, = matches
        checked_file(path, digest)
        if package == "pymupdf":
            verify_pymupdf_build_metadata(path)
        inputs["wheels/" + path.name] = (path, digest)
    if runtime_dir is not None and set(cached_wheels) != {path for name, (path, _) in inputs.items() if name.startswith("wheels/")}:
        raise ValueError("The runtime cache contains unpinned wheels")

    return write_pinned_archive(destination, inputs)


def write_pinned_archive(destination, inputs, compression=ZIP_STORED):
    with ZipFile(destination, "w", compression=compression, compresslevel=6) as archive:
        for name, (path, _) in sorted(inputs.items()):
            entry = ZipInfo(name)
            entry.compress_type = compression
            entry.external_attr = 0o100644 << 16
            with path.open("rb") as source, archive.open(entry, "w") as output:
                shutil.copyfileobj(source, output)
    with destination.open("rb") as stream:
        digest = hashlib.file_digest(stream, "sha256").hexdigest()
    return digest, {name: sha256 for name, (_, sha256) in sorted(inputs.items())}


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Remove local build metadata from a PyMuPDF wheel.")
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    clean_pymupdf_wheel(args.source, args.destination)
