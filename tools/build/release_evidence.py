"""Technical integrity of explicitly approved public evidence; no legal decision."""

import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath

# Exclude release-policy.json: it contains the evidence digests, so including
# its own digest inside the source evidence would create a circular reference.
SOURCE_LOCKS = (
    "requirements/windows-py314.lock", "packaging/python-wheels.json",
    "packaging/notices-lock.json", "packaging/vendor-lock.json",
    "packaging/build-environment.json", "packaging/source-assets.json",
    "build.ps1", "packaging/magic_converter.spec", "tools/build/build_inventory.py",
    "tools/build/release_evidence.py", "packaging/app.manifest", "packaging/version_info.txt",
    "magic_converter/__init__.py", "run_magic_converter.py",
)


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def evidence(root, name, expected):
    if not isinstance(name, str) or not name or Path(name).is_absolute():
        raise ValueError("Invalid public evidence path")
    path = root / name
    if (not path.resolve().is_relative_to(root) or path.is_symlink()
            or not path.is_file() or path.stat().st_size == 0):
        raise ValueError("Missing/empty/unsafe public evidence: " + name)
    if not isinstance(expected, str) or not re.fullmatch(r"[a-f0-9]{64}", expected) or digest(path) != expected:
        raise ValueError("Public evidence SHA256 differs: " + name)
    return path


def member_name(name):
    relative = PurePosixPath(name)
    if (not name or "\\" in name or ":" in name or relative.is_absolute()
            or ".." in relative.parts or str(relative) != name.rstrip("/")):
        raise ValueError("Unsafe corresponding-source member: " + name)
    return name


def validate_public_evidence(root, policy):
    from magic_converter import __version__
    from tools.build.build_inventory import package_metadata

    root = Path(root).resolve()
    license_path = evidence(root, policy.get("project_license_file"), policy.get("project_license_sha256"))
    license_text = license_path.read_text(encoding="utf-8-sig")
    if not license_text.strip() or "\x00" in license_text:
        raise ValueError("Project license evidence is not nonempty UTF-8 text")
    source = evidence(root, policy.get("corresponding_source_package"), policy.get("corresponding_source_sha256"))
    manifest_path = evidence(root, policy.get("corresponding_source_manifest"), policy.get("corresponding_source_manifest_sha256"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    locks = {name: digest(root / name) for name in SOURCE_LOCKS}
    if (manifest.get("schema_version") != 1 or manifest.get("project_version") != __version__
            or manifest.get("build_locks") != locks):
        raise ValueError("Corresponding-source version/build inputs differ")
    files = {}
    for entry in manifest["files"]:
        name = member_name(entry["path"])
        if (name in files or name.endswith("/") or type(entry["bytes"]) is not int or entry["bytes"] <= 0
                or not re.fullmatch(r"[a-f0-9]{64}", entry["sha256"])):
            raise ValueError("Invalid/duplicate source manifest file: " + name)
        files[name] = entry
    if not files:
        raise ValueError("Corresponding-source manifest is empty")
    with zipfile.ZipFile(source) as archive:
        members = {}
        seen = set()
        for info in archive.infolist():
            name = member_name(info.filename)
            key = name.rstrip("/").casefold()
            if key in seen or stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 1:
                raise ValueError("Duplicate/encrypted/symlink source member: " + name)
            seen.add(key)
            if not info.is_dir():
                members[name] = info
        if set(members) != set(files):
            raise ValueError("Corresponding-source archive coverage differs")
        for name, entry in files.items():
            if members[name].file_size != entry["bytes"]:
                raise ValueError("Corresponding-source size differs: " + name)
            with archive.open(members[name]) as stream:
                actual = hashlib.file_digest(stream, "sha256").hexdigest()
            if actual != entry["sha256"]:
                raise ValueError("Corresponding-source checksum differs: " + name)
        for name, expected in {**locks, "LICENSE": digest(license_path)}.items():
            if name not in files or files[name]["sha256"] != expected:
                raise ValueError("Missing/changed corresponding project input: " + name)
    packages, _, _ = package_metadata(root)
    expected_components = {p["SPDXID"]: p["versionInfo"] for p in packages if p["SPDXID"] != "SPDXRef-windows"}
    components, used = {}, set()
    for component in manifest["components"]:
        identity = component["id"]
        sources = component["files"]
        if (identity in components or not isinstance(sources, list) or not sources
                or len(sources) != len(set(sources)) or not set(sources) <= files.keys()
                or used.intersection(sources)):
            raise ValueError("Invalid/duplicate corresponding-source component: " + identity)
        used.update(sources)
        components[identity] = component["version"]
    if components != expected_components:
        raise ValueError("Corresponding-source component versions/coverage differ")
    return {"project_version": __version__, "source_files": len(files), "components": len(components)}
