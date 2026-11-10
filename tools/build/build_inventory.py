"""Fail-closed inputs and SPDX 2.3 provenance of pre-freeze PyInstaller inputs.

This does not claim exhaustive native subcomponent or legal review. Final
archive bytes and EXE hashes are separately attested by verify_release.
"""

import base64
import csv
import datetime
import hashlib
import importlib.metadata as metadata
import io
import json
import os
import platform
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from packaging.licenses import canonicalize_license_expression
from packaging.utils import canonicalize_name
from tools.dependencies.fetch_vendor import sha, verify as verify_vendor


def read(root, name):
    return json.loads((root / name).read_text(encoding="utf-8"))


def checked(root, name, digest):
    path = root / name
    if not path.resolve().is_relative_to(root.resolve()) or path.is_symlink():
        raise ValueError("Input escapes checkout: " + name)
    if not path.is_file() or sha(path) != digest:
        raise ValueError("Missing/changed reviewed input: " + name)
    return path


def validate_inputs(root=ROOT, wheelhouse=None):
    root = Path(root).resolve()
    config = read(root, "packaging/build-environment.json")
    wheels = read(root, "packaging/python-wheels.json")
    notices = read(root, "packaging/notices-lock.json")
    if (
        platform.python_version() != config["python"]
        or sys.platform != "win32"
        or platform.machine().lower() != "amd64"
    ):
        raise ValueError("Build requires Windows x64 Python " + config["python"])
    if (
        wheels["python"] != config["python"]
        or wheels["platform"] != config["platform"]
        or notices["python"]["version"] != config["python"]
    ):
        raise ValueError("Build lock platforms differ")
    if errors := verify_vendor(root):
        raise ValueError("Missing/changed vendor inputs: " + str(errors))
    for asset in read(root, "packaging/source-assets.json")["assets"]:
        for entry in asset["files"]:
            checked(root, entry["file"], entry["sha256"])
    checked(root, "packaging/schemas/spdx-2.3.json", config["spdx_schema_sha256"])
    checked(root, notices["python"]["notice"], notices["python"]["sha256"])
    if sha(Path(sys.base_prefix) / "LICENSE.txt") != notices["python"]["sha256"]:
        raise ValueError("Python license differs from reviewed interpreter")
    records = {r["name"]: r for r in notices["packages"]}
    locked = {w["name"]: w for w in wheels["wheels"]}
    from tools.build.lock_build_inputs import closure, requirements
    from packaging.requirements import Requirement

    dependency_set = closure(
        [
            *requirements(root / "requirements/build.txt"),
            Requirement("pip==" + locked["pip"]["version"]),
        ]
    )
    if set(dependency_set) != set(locked):
        raise ValueError("Requirements dependency closure differs from reviewed lock")
    if set(records) != set(locked):
        raise ValueError("Wheel and notice package sets differ")
    lock_text = (root / "requirements/windows-py314.lock").read_text(encoding="utf-8")
    pins = re.findall(
        r"^([\w.-]+)==(\S+)\s+--hash=sha256:([a-f0-9]{64})$", lock_text, re.M
    )
    if {canonicalize_name(n): (v, h) for n, v, h in pins} != {
        n: (w["version"], w["sha256"]) for n, w in locked.items()
    } or len(pins) != len(locked):
        raise ValueError("Pip hash lock differs from wheel manifest")
    wheelhouse = Path(
        wheelhouse
        or os.environ.get("MAGIC_BUILD_WHEELHOUSE", root / ".build-cache/wheels")
    )
    owners = {}
    for name, wheel in locked.items():
        record = records[name]
        dist = metadata.distribution(name)
        if dist.version != wheel["version"] or record["version"] != wheel["version"]:
            raise ValueError("Installed/notice version differs from lock: " + name)
        checked(root, record["metadata"], record["metadata_sha256"])
        if dist.read_text("METADATA") != (root / record["metadata"]).read_text(
            encoding="utf-8"
        ):
            raise ValueError("Installed metadata differs from review: " + name)
        for entry in record["notices"]:
            checked(root, entry["file"], entry["sha256"])
            if sha(Path(dist.locate_file(entry["source"]))) != entry["sha256"]:
                raise ValueError("Installed original notice changed: " + name)
        archive_path = wheelhouse / wheel["filename"]
        if (
            not archive_path.is_file()
            or archive_path.stat().st_size != wheel["bytes"]
            or sha(archive_path) != wheel["sha256"]
        ):
            raise ValueError("Missing/changed locked wheel: " + str(archive_path))
        with zipfile.ZipFile(archive_path) as archive:
            record_name = next(
                n for n in archive.namelist() if n.endswith(".dist-info/RECORD")
            )
            hashes = {
                n: h
                for n, h, _ in csv.reader(
                    io.StringIO(archive.read(record_name).decode("utf-8"))
                )
                if h
            }
        for member in dist.files or ():
            path = Path(dist.locate_file(member)).resolve()
            original = str(member).replace("\\", "/")
            digest = hashes.get(original)
            if digest is None:
                options = [
                    h
                    for n, h in hashes.items()
                    if n.endswith(".data/purelib/" + original)
                    or n.endswith(".data/platlib/" + original)
                ]
                digest = options[0] if len(options) == 1 else None
            owners.setdefault(path, []).append((name, original, digest))
    return config, wheels, notices, owners


def source_owner(path, root, owners):
    path = Path(path).resolve()
    if path in owners:
        data = path.read_bytes()
        for name, member, expected in owners[path]:
            if not expected:
                continue
            algorithm, encoded = expected.split("=", 1)
            digest = (
                base64.urlsafe_b64encode(hashlib.new(algorithm, data).digest())
                .rstrip(b"=")
                .decode("ascii")
            )
            if digest == encoded:
                return name, member
        raise ValueError("Bundled package file differs from locked wheel: " + str(path))
    if "site-packages" in [p.lower() for p in path.parts]:
        raise ValueError("Unowned/unlocked bundled package file: " + str(path))
    if path.is_relative_to(root) and not path.is_relative_to(root / ".venv"):
        return "project", path.relative_to(root).as_posix()
    base = Path(sys.base_prefix).resolve()
    if path.is_relative_to(base):
        return "python", path.relative_to(base).as_posix()
    windows = Path(os.environ.get("SystemRoot", "C:/Windows")).resolve()
    if path.is_relative_to(windows):
        return "windows", path.relative_to(windows).as_posix()
    raise ValueError("Unknown bundled input origin: " + str(path))


def validate_sbom(document, root=ROOT):
    from jsonschema import Draft7Validator

    schema = read(root, "packaging/schemas/spdx-2.3.json")
    Draft7Validator(schema).validate(document)
    identifiers = (
        {p["SPDXID"] for p in document["packages"]}
        | {f["SPDXID"] for f in document["files"]}
        | {"SPDXRef-DOCUMENT"}
    )
    if len(identifiers) != len(document["packages"]) + len(document["files"]) + 1:
        raise ValueError("Duplicate SPDX identity")
    for rel in document["relationships"]:
        if (
            rel["spdxElementId"] not in identifiers
            or rel["relatedSpdxElement"] not in identifiers
        ):
            raise ValueError("Unresolved SPDX relationship")


def package_metadata(root=ROOT):
    """Derive package claims only from the reviewed manifests and notices."""
    from magic_converter import __version__

    root = Path(root)
    config = read(root, "packaging/build-environment.json")
    wheels = read(root, "packaging/python-wheels.json")
    notices = read(root, "packaging/notices-lock.json")
    packages, extracted = [], []

    def package(
        key,
        name,
        version,
        url="NOASSERTION",
        digest=None,
        declared="NOASSERTION",
        comment="",
    ):
        item = {
            "SPDXID": "SPDXRef-" + key,
            "name": name,
            "versionInfo": version,
            "downloadLocation": url,
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": declared,
            "copyrightText": "NOASSERTION",
        }
        if digest:
            item["checksums"] = [{"algorithm": "SHA256", "checksumValue": digest}]
        if comment:
            item["comment"] = comment
        packages.append(item)
        return item

    package(
        "project",
        "magic-converter",
        __version__,
        comment="Internal review build. Project license and redistribution/source obligations unresolved; see release-policy.json.",
    )
    package(
        "python",
        "CPython",
        config["python"],
        "https://www.python.org/downloads/release/python-3147/",
        declared="Python-2.0",
    )
    package(
        "windows",
        "Windows runtime files",
        platform.version(),
        comment="Observed system files only; no exhaustive OS inventory.",
    )
    notice_map = {p["name"]: p for p in notices["packages"]}
    for wheel in wheels["wheels"]:
        record = notice_map[wheel["name"]]
        expression = (
            record["declared_expression"] or record["declared_license"] or "NOASSERTION"
        )
        try:
            expression = str(canonicalize_license_expression(expression))
        except Exception:
            expression = "LicenseRef-" + wheel["name"]
            text = "\n\n".join(
                (root / n["file"]).read_text(encoding="utf-8", errors="replace")
                for n in record["notices"]
            )
            extracted.append(
                {
                    "licenseId": expression,
                    "extractedText": text,
                    "name": "Original notices for " + wheel["name"],
                }
            )
        item = package(
            wheel["name"],
            wheel["name"],
            wheel["version"],
            wheel["url"],
            wheel["sha256"],
            expression,
            "Upstream license metadata is not a selected license/entitlement. Full notices: licenses/runtime/. Embedded native libraries are not exhaustively decomposed.",
        )
        item["externalRefs"] = [
            {
                "referenceCategory": "PACKAGE-MANAGER",
                "referenceType": "purl",
                "referenceLocator": "pkg:pypi/"
                + wheel["name"]
                + "@"
                + wheel["version"],
            }
        ]
    vendor_owners = {}
    for index, artifact in enumerate(
        read(root, "packaging/vendor-lock.json")["artifacts"]
    ):
        key = "vendor-" + str(index)
        declared = artifact["license"]
        if declared.startswith("LicenseRef-"):
            extracted.append(
                {
                    "licenseId": declared,
                    "extractedText": "\n\n".join(
                        (root / f["target"]).read_text(
                            encoding="utf-8", errors="replace"
                        )
                        for f in artifact["files"]
                        if "LICENSE" in f["target"]
                    ),
                    "name": artifact["id"],
                }
            )
        package(
            key,
            artifact["id"],
            artifact["version"],
            artifact["url"],
            artifact["sha256"],
            declared,
            "Pinned archive. Source URLs in vendor-lock.json are references, not a complete corresponding-source distribution.",
        )
        vendor_owners.update(
            {
                (root / e["target"]).resolve(): (key, e["target"])
                for e in artifact["files"]
            }
        )
    for index, asset in enumerate(read(root, "packaging/source-assets.json")["assets"]):
        key = "source-asset-" + str(index)
        declared = asset["license"]
        if declared.startswith("LicenseRef-"):
            extracted.append(
                {
                    "licenseId": declared,
                    "extractedText": asset["license_status"],
                    "name": asset["name"],
                }
            )
        package(
            key,
            asset["name"],
            asset["version"],
            asset["source_url"],
            declared=declared,
            comment=asset["license_status"],
        )
        vendor_owners.update(
            {(root / e["file"]).resolve(): (key, e["file"]) for e in asset["files"]}
        )
    return packages, extracted, vendor_owners


def validate_package_metadata(sbom, root=ROOT):
    packages, extracted, _ = package_metadata(root)
    expected = {p["SPDXID"]: p for p in packages}
    actual = {p["SPDXID"]: p for p in sbom["packages"]}
    if len(actual) != len(sbom["packages"]) or actual != expected:
        raise ValueError("SPDX package claims differ from reviewed inputs")
    if sbom.get("hasExtractedLicensingInfos", []) != extracted:
        raise ValueError("SPDX extracted licenses differ from reviewed notices")


def create(analysis, root, output):
    root, output = Path(root).resolve(), Path(output).resolve()
    config, wheels, notices, owners = validate_inputs(root)
    if (
        os.environ.get("SOURCE_DATE_EPOCH") != str(config["source_date_epoch"])
        or os.environ.get("PYTHONHASHSEED") != config["pythonhashseed"]
    ):
        raise ValueError("Use build.ps1: reproducible environment is not set")
    from magic_converter import __version__

    packages, extracted, vendor_owners = package_metadata(root)
    relationships = []
    import PyInstaller
    from PyInstaller.depend.analysis import get_bootstrap_modules

    bootloader = Path(PyInstaller.__file__).parent / "bootloader/Windows-64bit-intel"
    inventory, files, shipped, seen, namespaces = [], [], set(), set(), []
    for group, entries in (
        ("pure", analysis.pure),
        ("binary", analysis.binaries),
        ("data", analysis.datas),
        ("script", analysis.scripts),
        ("bootstrap", get_bootstrap_modules()),
        (
            "bootloader",
            [(n, str(bootloader / n), "BOOTLOADER") for n in ("run.exe", "runw.exe")],
        ),
    ):
        for target, source, kind in sorted(entries):
            if group == "pure" and source == "-":
                candidates = {
                    name
                    for records in owners.values()
                    for name, member, _ in records
                    if member.startswith(target.replace(".", "/") + "/")
                }
                if len(candidates) != 1:
                    raise ValueError("Unknown virtual namespace owner: " + target)
                owner = candidates.pop()
                namespaces.append({"module": target, "owner": owner})
                shipped.add(owner)
                continue
            path = Path(source).resolve()
            if not path.is_file():
                raise ValueError("Bundled input is missing: " + source)
            owner, member = vendor_owners.get(path) or source_owner(path, root, owners)
            target = target.replace("\\", "/")
            identity = group + ":" + target
            if identity in seen:
                raise ValueError("Duplicate bundle target: " + identity)
            seen.add(identity)
            digest = sha(path)
            fid = "SPDXRef-file-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
            inventory.append(
                {
                    "target": target,
                    "group": group,
                    "kind": kind,
                    "owner": owner,
                    "source": member,
                    "sha256": digest,
                    "bytes": path.stat().st_size,
                }
            )
            files.append(
                {
                    "SPDXID": fid,
                    "fileName": "./" + group + "/" + target,
                    "checksums": [{"algorithm": "SHA256", "checksumValue": digest}],
                    "licenseConcluded": "NOASSERTION",
                    "copyrightText": "NOASSERTION",
                    "comment": "Pre-freeze source checksum; final archive may transform bytecode/PE. Origin: "
                    + owner
                    + "/"
                    + member,
                }
            )
            relationships.append(
                {
                    "spdxElementId": "SPDXRef-" + owner,
                    "relationshipType": "CONTAINS",
                    "relatedSpdxElement": fid,
                }
            )
            shipped.add(owner)
    for item in packages[1:]:
        key = item["SPDXID"][8:]
        relationships.append(
            {
                "spdxElementId": item["SPDXID"],
                "relationshipType": (
                    "DEPENDENCY_OF" if key in shipped else "BUILD_DEPENDENCY_OF"
                ),
                "relatedSpdxElement": "SPDXRef-project",
            }
        )
    fingerprints = {
        name: sha(root / name)
        for name in [
            "requirements/windows-py314.lock",
            "packaging/python-wheels.json",
            "packaging/notices-lock.json",
            "packaging/vendor-lock.json",
            "packaging/build-environment.json",
            "packaging/release-policy.json",
            "packaging/source-assets.json",
            "build.ps1",
            "packaging/magic_converter.spec",
            "tools/build/build_inventory.py",
            "tools/dependencies/fetch_vendor.py",
            "tools/build/release_evidence.py",
        ]
    }
    payload = {
        "schema_version": 1,
        "version": __version__,
        "scope": "pre-freeze PyInstaller inputs; native-subcomponent/policy review incomplete",
        "locks": fingerprints,
        "files": inventory,
        "namespace_modules": namespaces,
    }
    identity = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "magic-converter-" + __version__ + "-build-inputs",
        "documentNamespace": "https://magic-converter.invalid/spdx/" + identity,
        "creationInfo": {
            "creators": ["Tool: magic-converter-build-inventory-1"],
            "created": datetime.datetime.fromtimestamp(
                config["source_date_epoch"], datetime.timezone.utc
            ).strftime("%Y-%m-%dT%H:%M:%SZ"),
        },
        "packages": packages,
        "files": files,
        "relationships": [
            {
                "spdxElementId": "SPDXRef-DOCUMENT",
                "relationshipType": "DESCRIBES",
                "relatedSpdxElement": "SPDXRef-project",
            }
        ]
        + relationships,
    }
    if extracted:
        document["hasExtractedLicensingInfos"] = extracted
    validate_sbom(document, root)
    output.mkdir(parents=True, exist_ok=True)
    for name, data in (("build-inputs.json", payload), ("sbom.spdx.json", document)):
        (output / name).write_text(
            json.dumps(data, ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
    return [
        (str(output / name), "build-metadata")
        for name in ("build-inputs.json", "sbom.spdx.json")
    ]


def public_gate(root=ROOT):
    policy = read(root, "packaging/release-policy.json")
    if (
        policy["public_release_approved"] is not True
        or policy["obligations_reviewed"] is not True
        or not policy["project_license_file"]
        or not policy["corresponding_source_package"]
    ):
        raise ValueError(
            "Public release policy unresolved; internal builds only. See packaging/release-policy.json"
        )
    from tools.build.release_evidence import validate_public_evidence

    return validate_public_evidence(root, policy)


def verify_frozen_input(entry, data, target):
    if hashlib.sha256(data).hexdigest() != entry["sha256"] or len(data) != entry["bytes"]:
        raise ValueError("Frozen input bytes differ: " + target)


def verify_archive(archive, root=ROOT):
    """Check embedded provenance, full notices and every observed archive file."""
    names = {key.replace("\\", "/"): key for key in archive.toc}

    def extract(name):
        return archive.extract(names[name])

    payload = json.loads(extract("build-metadata/build-inputs.json"))
    sbom = json.loads(extract("build-metadata/sbom.spdx.json"))
    validate_sbom(sbom, root)
    validate_package_metadata(sbom, root)
    from magic_converter import __version__
    required_locks = {
        "requirements/windows-py314.lock", "packaging/python-wheels.json",
        "packaging/notices-lock.json", "packaging/vendor-lock.json",
        "packaging/build-environment.json", "packaging/release-policy.json",
        "packaging/source-assets.json", "build.ps1", "packaging/magic_converter.spec",
        "tools/build/build_inventory.py", "tools/dependencies/fetch_vendor.py",
        "tools/build/release_evidence.py",
    }
    if (payload.get("schema_version") != 1 or payload.get("version") != __version__
            or set(payload["locks"]) != required_locks):
        raise ValueError("Build input version/schema/lock coverage differs")
    for name, digest in payload["locks"].items():
        checked(root, name, digest)
    files = payload["files"]
    spdx_checksums = {
        f["fileName"]: f["checksums"][0]["checksumValue"] for f in sbom["files"]
    }
    if spdx_checksums != {
        "./" + f["group"] + "/" + f["target"]: f["sha256"] for f in files
    }:
        raise ValueError("SPDX file list differs from build inventory")
    expected = {f["target"]: f for f in files if f["group"] in ("data", "binary")}
    expected.update(
        {
            n: None
            for n in (
                "build-metadata/build-inputs.json",
                "build-metadata/sbom.spdx.json",
            )
        }
    )
    actual = {n for n, key in names.items() if archive.toc[key][-1] in ("b", "x", "n")}
    if actual != set(expected):
        raise ValueError(
            "Final archive coverage differs: " + str(sorted(actual ^ set(expected)))
        )
    bootstrap_modules = {
        f["target"]
        for f in files
        if f["group"] == "bootstrap" and f["kind"].startswith("PYMODULE")
    }
    expected_pure = {f["target"] for f in files if f["group"] == "pure"} | {
        n["module"] for n in payload["namespace_modules"]
    }
    pyz_name = next(n for n, key in names.items() if archive.toc[key][-1] == "z")
    if (
        set(archive.open_embedded_archive(names[pyz_name]).toc)
        != expected_pure - bootstrap_modules
    ):
        raise ValueError("Final Python module coverage differs from inventory")
    expected_scripts = {
        f["target"]
        for f in files
        if f["group"] in ("script", "bootstrap") and f["kind"].startswith("PYSOURCE")
    }
    actual_scripts = {n for n, key in names.items() if archive.toc[key][-1] == "s"}
    actual_modules = {
        n for n, key in names.items() if archive.toc[key][-1] in ("m", "M")
    }
    if actual_scripts != expected_scripts or actual_modules != bootstrap_modules:
        raise ValueError("Final bootstrap/script coverage differs from inventory")
    records = []
    for target in sorted(names):
        data = extract(target)
        if not isinstance(data, bytes):
            raise ValueError("Unsupported final archive entry: " + target)
        digest = hashlib.sha256(data).hexdigest()
        entry = expected.get(target)
        # This spec disables strip/UPX and the reviewed build needs no PE
        # rewriting. Any future transformation needs a pinned expected result.
        if entry:
            verify_frozen_input(entry, data, target)
        records.append(
            {
                "file": target,
                "sha256": digest,
                "bytes": len(data),
                "source_sha256": entry["sha256"] if entry else None,
                "owner": entry["owner"] if entry else "build-metadata",
            }
        )
    notices = read(root, "packaging/notices-lock.json")
    required = [n for p in notices["packages"] for n in p["notices"]]
    required += [
        {"file": p["metadata"], "sha256": p["metadata_sha256"]}
        for p in notices["packages"]
    ]
    required += [
        {"file": notices["python"]["notice"], "sha256": notices["python"]["sha256"]}
    ]
    for entry in required:
        if hashlib.sha256(extract(entry["file"])).hexdigest() != entry["sha256"]:
            raise ValueError("Frozen original notice differs: " + entry["file"])
    return {
        "spdx_packages": len(sbom["packages"]),
        "input_files": len(files),
        "original_notice_files": len(required),
        "archive_files": records,
    }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--public", action="store_true")
    args = parser.parse_args()
    if args.public:
        public_gate()
    else:
        validate_inputs()
    print(
        "Public release evidence verified"
        if args.public
        else "Reviewed build inputs verified"
    )
