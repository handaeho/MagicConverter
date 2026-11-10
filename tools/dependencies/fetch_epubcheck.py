"""Pinned test-only EPUBCheck; never bundled with the app."""

import argparse
import json
import sys
import zipfile
from pathlib import Path, PurePosixPath

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.dependencies.fetch_vendor import cached_archive


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    lock = json.loads(
        (ROOT / "packaging/validators-lock.json").read_text(encoding="utf-8")
    )
    cache = ROOT / ".build-cache/validators"
    cache.mkdir(parents=True, exist_ok=True)
    archive = cached_archive(lock, cache, args.offline)
    with zipfile.ZipFile(archive) as z:
        if len(z.namelist()) != len(set(z.namelist())):
            raise ValueError("Duplicate validator members")
        for info in z.infolist():
            name = PurePosixPath(info.filename)
            target = cache / name
            if (
                name.is_absolute()
                or ".." in name.parts
                or not target.resolve().is_relative_to(cache.resolve())
                or (info.external_attr >> 16) & 0o170000 == 0o120000
            ):
                raise ValueError("Unsafe validator member")
            if info.is_dir():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(z.read(info))
    print(cache / "epubcheck-5.4.0/epubcheck.jar")


if __name__ == "__main__":
    main()
