"""Explicit maintenance command: freeze reviewed installed versions and wheels.

Normal builds consume the committed lock; they never refresh it automatically.
"""

import argparse
import hashlib
import importlib.metadata as metadata
import json
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name, parse_wheel_filename
from packaging.tags import sys_tags

ROOT = Path(__file__).resolve().parents[2]


def requirements(path):
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        if line.startswith("-r "):
            yield from requirements(path.parent / line[3:].strip())
        elif line:
            yield Requirement(line)


def closure(roots):
    selected, extras, pending = {}, {}, list(roots)
    environment = default_environment()
    while pending:
        requirement = pending.pop()
        name = canonicalize_name(requirement.name)
        if requirement.marker and not requirement.marker.evaluate(environment):
            continue
        dist = metadata.distribution(name)
        if requirement.specifier and dist.version not in requirement.specifier:
            raise RuntimeError(
                f"Installed {name} {dist.version} violates {requirement}"
            )
        expanded = set(requirement.extras) | extras.get(name, set()) | {""}
        if name in selected and expanded == extras[name]:
            continue
        selected[name], extras[name] = dist, expanded
        for value in dist.requires or ():
            child = Requirement(value)
            if child.marker and not any(
                child.marker.evaluate({**environment, "extra": extra})
                for extra in expanded
            ):
                continue
            child.marker = None
            pending.append(child)
    return selected


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--wheelhouse", type=Path, default=ROOT / ".build-cache/wheels")
    args = parser.parse_args()
    selected = closure(
        [
            *requirements(ROOT / "requirements/build.txt"),
            Requirement("pip==" + metadata.version("pip")),
        ]
    )
    args.wheelhouse.mkdir(parents=True, exist_ok=True)
    pins = [name + "==" + dist.version for name, dist in sorted(selected.items())]
    subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "download",
            "--no-deps",
            "--only-binary=:all:",
            "--dest",
            str(args.wheelhouse),
            *pins,
        ],
        check=True,
    )
    compatible = set(sys_tags())
    records = []
    for name, dist in sorted(selected.items()):
        candidates = []
        for wheel in args.wheelhouse.glob("*.whl"):
            wheel_name, version, _, tags = parse_wheel_filename(wheel.name)
            if (
                wheel_name == name
                and str(version) == dist.version
                and tags & compatible
            ):
                candidates.append(wheel)
        if len(candidates) != 1:
            raise RuntimeError(
                f"Need exactly one compatible reviewed wheel for {name}: {candidates}"
            )
        wheel = candidates[0]
        with wheel.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        with urllib.request.urlopen(
            f"https://pypi.org/pypi/{name}/{dist.version}/json", timeout=30
        ) as stream:
            release = json.load(stream)
        upstream = next(
            item for item in release["urls"] if item["filename"] == wheel.name
        )
        if digest != upstream["digests"]["sha256"]:
            raise RuntimeError("Wheel differs from PyPI release: " + wheel.name)
        records.append(
            {
                "name": name,
                "version": dist.version,
                "filename": wheel.name,
                "sha256": digest,
                "url": upstream["url"],
                "bytes": wheel.stat().st_size,
                "source_archives": [
                    {
                        "url": f["url"],
                        "sha256": f["digests"]["sha256"],
                        "filename": f["filename"],
                    }
                    for f in release["urls"]
                    if f["packagetype"] == "sdist"
                ],
            }
        )
    lock = (
        "# Windows x64 / CPython "
        + platform.python_version()
        + "; refresh only after review.\n"
    )
    lock += (
        "\n".join(
            f'{r["name"]}=={r["version"]} --hash=sha256:{r["sha256"]}' for r in records
        )
        + "\n"
    )
    (ROOT / "requirements/windows-py314.lock").write_text(lock, encoding="utf-8")
    (ROOT / "packaging/python-wheels.json").write_text(
        json.dumps(
            {
                "python": platform.python_version(),
                "platform": "win_amd64",
                "wheels": records,
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print("Locked", len(records), "reviewed build/runtime wheels")


if __name__ == "__main__":
    main()
