"""Stage the pinned Windows HEIF libraries from a local MSYS2 package cache."""

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import tarfile
import tempfile


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def prepare(lock, cache, output):
    if output.exists():
        raise ValueError("Choose a new HEIF staging directory.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as directory:
        staged = Path(directory) / "runtime"
        staged.mkdir()
        for package in lock["packages"].values():
            archive = cache / package["file"]
            if not archive.is_file() or digest(archive) != package["sha256"]:
                raise ValueError(f"Missing or mismatched package: {lock['source']}{package['file']}")
            expected = {f"ucrt64/bin/{name}": sha for name, sha in package["dlls"].items()}
            expected.update({f"ucrt64/include/{name}": sha
                             for name, sha in package.get("headers", {}).items()})
            with tarfile.open(archive, "r:zst") as tar:
                for member in tar:
                    name = PurePosixPath(member.name)
                    if not member.isfile():
                        continue
                    sha = expected.pop(str(name), None)
                    if sha and name.parts[1] == "include":
                        target = staged / "include" / Path(*name.parts[2:])
                    elif sha:
                        target = staged / name.name
                    elif (name.parts[:3] == ("ucrt64", "share", "licenses")
                          and len(name.parts) >= 5 and ".." not in name.parts):
                        target = staged / "licenses" / Path(*name.parts[3:])
                    else:
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    with tar.extractfile(member) as source, target.open("xb") as result:
                        shutil.copyfileobj(source, result)
                    if sha and digest(target) != sha:
                        raise ValueError(f"Mismatched HEIF library: {name}")
            if expected:
                raise ValueError(f"Missing HEIF libraries in {archive.name}: {', '.join(expected)}")
        staged.rename(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cache", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    runtime = json.loads(Path(__file__).with_name("rust_heif_runtime.json").read_bytes())
    prepare(runtime, args.cache, args.output.resolve())
