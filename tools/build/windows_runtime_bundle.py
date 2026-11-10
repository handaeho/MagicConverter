"""Collect and verify the runtime bytes embedded in the Windows installer."""

import hashlib
import json
import shutil
from urllib.request import urlopen
from zipfile import ZIP_STORED, ZipFile, ZipInfo


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
            matches = [path for path in cached_wheels if path.name.startswith(prefix)]
            if len(matches) != 1:
                raise ValueError(f"Expected one cached wheel for {requirement}")
            path, = matches
        inputs["wheels/" + path.name] = (checked_file(path, digest), digest)
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
