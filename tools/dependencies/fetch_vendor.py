"""Acquire only manifest-listed vendor files, validating archive and member bytes."""

import argparse
import hashlib
import io
import json
import os
import re
import shutil
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def targets(root, manifest):
    if not (root / "vendor").resolve().is_relative_to(root.resolve()):
        raise ValueError("Vendor directory escapes workspace")
    result = {}
    for artifact in manifest["artifacts"]:
        if (
            not re.fullmatch(r"[a-f0-9]{64}", artifact["sha256"])
            or Path(artifact["filename"]).name != artifact["filename"]
        ):
            raise ValueError("Invalid archive identity")
        for entry in artifact["files"]:
            relative = PurePosixPath(entry["target"])
            path = root / relative
            if (
                relative.is_absolute()
                or ".." in relative.parts
                or relative.parts[0] != "vendor"
                or not path.resolve().is_relative_to((root / "vendor").resolve())
            ):
                raise ValueError("Vendor target escapes vendor directory")
            if (
                str(relative) in result
                or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])
                or not 0 < entry["bytes"] < 2**31
            ):
                raise ValueError("Duplicate or invalid vendor member")
            result[str(relative)] = entry
    return result


def verify(root=ROOT, manifest=None):
    root = Path(root).resolve()
    manifest = manifest or json.loads(
        (root / "packaging/vendor-lock.json").read_text(encoding="utf-8")
    )
    entries = targets(root, manifest)
    errors = []
    for name, entry in entries.items():
        path = root / name
        if (
            not path.is_file()
            or path.is_symlink()
            or path.stat().st_size != entry["bytes"]
            or sha(path) != entry["sha256"]
        ):
            errors.append(name)
    return errors


def cached_archive(artifact, cache, offline):
    path = cache / (artifact["sha256"] + "-" + artifact["filename"])
    if path.exists():
        if (
            path.is_symlink()
            or path.stat().st_size != artifact["bytes"]
            or sha(path) != artifact["sha256"]
        ):
            raise ValueError("Corrupt vendor cache: " + str(path))
        return path
    if offline:
        raise FileNotFoundError("Offline vendor archive is missing: " + str(path))
    if not artifact["url"].startswith("https://"):
        raise ValueError("Vendor downloads require HTTPS")
    descriptor, temporary = tempfile.mkstemp(
        prefix="download-", suffix=".part", dir=cache
    )
    temporary = Path(temporary)
    try:
        digest, total = hashlib.sha256(), 0
        with os.fdopen(descriptor, "wb") as output, urllib.request.urlopen(
            artifact["url"], timeout=45
        ) as source:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > artifact["bytes"]:
                    raise ValueError("Vendor archive exceeds locked size")
                digest.update(chunk)
                output.write(chunk)
        if total != artifact["bytes"] or digest.hexdigest() != artifact["sha256"]:
            raise ValueError("Vendor archive SHA256 mismatch")
        temporary.replace(path)
        return path
    finally:
        temporary.unlink(missing_ok=True)


def copy_checked(source, destination, entry):
    digest, total = hashlib.sha256(), 0
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("wb") as output:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if total > entry["bytes"]:
                raise ValueError("Expanded vendor member exceeds locked size")
            digest.update(chunk)
            output.write(chunk)
    if total != entry["bytes"] or digest.hexdigest() != entry["sha256"]:
        raise ValueError("Vendor member SHA256 mismatch: " + entry["target"])


def unpack(artifact, archive, stage):
    if artifact["format"] == "file":
        if len(artifact["files"]) != 1:
            raise ValueError("Direct vendor asset must have one target")
        entry = artifact["files"][0]
        with archive.open("rb") as source:
            copy_checked(source, stage / entry["target"], entry)
        return
    with zipfile.ZipFile(archive) as package:
        names = package.namelist()
        if len(names) != len(set(names)):
            raise ValueError("Duplicate vendor ZIP members")
        if artifact["format"] == "zip":
            for entry in artifact["files"]:
                info = package.getinfo(entry["member"])
                if (
                    info.file_size != entry["bytes"]
                    or (info.external_attr >> 16) & 0o170000 == 0o120000
                ):
                    raise ValueError("Invalid ZIP member size or symlink")
                with package.open(info) as source:
                    copy_checked(source, stage / entry["target"], entry)
        elif artifact["format"] == "conda":
            from compression import zstd

            for container in sorted({e["container"] for e in artifact["files"]}):
                # This pinned VC package has a small expanded tar; bound the
                # decompressor independently of its advertised archive length.
                with zstd.open(io.BytesIO(package.read(container)), "rb") as stream:
                    data = stream.read(64 * 1024 * 1024 + 1)
                if len(data) > 64 * 1024 * 1024:
                    raise ValueError("Vendor tar exceeds expansion budget")
                with tarfile.open(fileobj=io.BytesIO(data)) as contents:
                    members = contents.getmembers()
                    if len({m.name for m in members}) != len(members):
                        raise ValueError("Duplicate vendor tar members")
                    for entry in (
                        e for e in artifact["files"] if e["container"] == container
                    ):
                        info = contents.getmember(entry["member"])
                        if not info.isfile() or info.size != entry["bytes"]:
                            raise ValueError("Invalid vendor tar member")
                        with contents.extractfile(info) as source:
                            copy_checked(source, stage / entry["target"], entry)
        else:
            raise ValueError("Unsupported vendor archive format")


def acquire(root=ROOT, cache=None, *, offline=False, repair=False, manifest=None):
    root = Path(root).resolve()
    cache = Path(cache or root / ".build-cache/vendor").resolve()
    cache.mkdir(parents=True, exist_ok=True)
    manifest = manifest or json.loads(
        (root / "packaging/vendor-lock.json").read_text(encoding="utf-8")
    )
    entries = targets(root, manifest)
    missing = set(verify(root, manifest))
    for name in missing:
        if (root / name).exists() and not repair:
            raise ValueError(
                "Existing vendor file differs from lock; review before --repair: "
                + name
            )
    with tempfile.TemporaryDirectory(prefix="vendor-stage-", dir=cache) as temporary:
        stage = Path(temporary).resolve()
        if not stage.is_relative_to(cache):
            raise ValueError("Unexpected private vendor stage")
        for artifact in manifest["artifacts"]:
            if not any(e["target"] in missing for e in artifact["files"]):
                continue
            archive = cached_archive(artifact, cache, offline)
            unpack(artifact, archive, stage)
        # All archive/member checks finish before publishing any replacement.
        for name in sorted(missing):
            targets(root, manifest)
            target = root / name
            if not target.resolve().is_relative_to((root / "vendor").resolve()):
                raise ValueError("Vendor target changed during extraction")
            target.parent.mkdir(parents=True, exist_ok=True)
            descriptor, temporary_file = tempfile.mkstemp(
                prefix=".vendor-", dir=target.parent
            )
            os.close(descriptor)
            try:
                shutil.copyfile(stage / name, temporary_file)
                os.replace(temporary_file, target)
            finally:
                Path(temporary_file).unlink(missing_ok=True)
    if errors := verify(root, manifest):
        raise ValueError("Vendor verification failed: " + str(errors))
    return len(entries)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--cache", type=Path)
    parser.add_argument("--offline", action="store_true")
    parser.add_argument("--repair", action="store_true")
    parser.add_argument("--verify", action="store_true")
    args = parser.parse_args()
    if args.verify:
        if errors := verify(args.root):
            raise SystemExit("Missing/changed vendor inputs: " + str(errors))
        print("Vendor input hashes verified")
    else:
        print(
            "Verified vendor files:",
            acquire(args.root, args.cache, offline=args.offline, repair=args.repair),
        )


if __name__ == "__main__":
    main()
