"""Reproduce stage-9 follow-up findings without modifying app/EXE inputs.

Archive injections are read-only wrappers, as in verify_build_failure_gates.
Exit 0 means the audit completed; --strict fails when an issue is observed.
"""

import argparse
import copy
import hashlib
import io
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter import __version__
from PIL import Image
from PyInstaller.archive.readers import CArchiveReader
from magic_converter.core.specialized.ebook_converter import (
    Book,
    Chapter,
    XHTML,
    read_book,
    write_epub,
)
from magic_converter.core.specialized.safe_html import image_uri
from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.conversion_request import prepare_request
from magic_converter.core.conversion.conversion_execution import execute_file
from tools.build.build_inventory import verify_archive, public_gate
from tools.validation.verify_build_failure_gates import InjectedArchive


def digest(data):
    return hashlib.sha256(data).hexdigest()


def epubcheck(path, output, jar, java):
    report = output / (path.stem + "-epubcheck.json")
    report.unlink(missing_ok=True)
    p = subprocess.run(
        [java, "-jar", str(jar), str(path), "--json", str(report)],
        capture_output=True,
        timeout=60,
    )
    data = json.loads(report.read_text(encoding="utf-8"))
    if p.returncode not in (0, 1) or not data.get("checker"):
        raise RuntimeError("EPUBCheck infrastructure failure")
    return {
        "exit": p.returncode,
        "checker": data["checker"],
        "messages": data.get("messages", []),
    }


def conversion(source):
    notices = []
    try:
        request = prepare_request([source], "document:epub", create_catalog())
        output = execute_file(
            request, request.files[0], None, lambda _: None, notices.append
        )
        return {"status": "passed", "output": str(output), "notices": notices}
    except Exception as exc:
        return {
            "status": "failed",
            "error": str(exc),
            "error_type": type(exc).__name__,
            "notices": notices,
        }


def book_cases(output, jar, java):
    records = []
    for tag in ("div", "section", "aside"):
        path = output / ("anchor-" + tag + ".epub")
        base = output / ("base-" + tag + ".epub")
        write_epub(
            Book(
                "Anchor",
                language="en",
                chapters=[
                    Chapter(
                        "Chapter",
                        '<p><a href="#note">Jump</a></p><div id="note"><p>Note</p></div>',
                    )
                ],
            ),
            base,
        )
        with zipfile.ZipFile(base) as source, zipfile.ZipFile(path, "w") as target:
            for entry in source.infolist():
                data = source.read(entry)
                if entry.filename == "EPUB/chapter0.xhtml":
                    doc = ET.fromstring(data)
                    next(n for n in doc.iter() if n.get("id") == "note").tag = (
                        f"{{{XHTML}}}" + tag
                    )
                    data = ET.tostring(doc, encoding="utf-8", xml_declaration=True)
                target.writestr(entry, data)
        validation = epubcheck(path, output, jar, java)
        if validation["exit"]:
            raise RuntimeError("Anchor fixture was not valid: " + tag)
        result = conversion(path)
        records.append(
            {
                "case": "EPUB ID on " + tag,
                "id": "A01" if tag != "div" else "control",
                "observed_issue": tag != "div" and result["status"] != "passed",
                "input": str(path),
                "input_check": validation,
                "conversion": result,
            }
        )
    path = output / "literal-percent-id.epub"
    write_epub(
        Book(
            "Percent",
            language="en",
            chapters=[
                Chapter(
                    "Chapter",
                    '<p id="p%20q">Literal percent</p><p><a href="#p%2520q">Jump</a></p>',
                )
            ],
        ),
        path,
    )
    validation = epubcheck(path, output, jar, java)
    if validation["exit"]:
        raise RuntimeError("Percent fixture was not valid")
    result = conversion(path)
    records.append(
        {
            "case": "EPUB percent-encoded fragment roundtrip",
            "id": "A02",
            "observed_issue": result["status"] != "passed",
            "input": str(path),
            "input_check": validation,
            "conversion": result,
        }
    )
    raw = io.BytesIO()
    Image.new("RGB", (10, 6), "blue").save(raw, "WEBP")
    path = output / "webp.md"
    path.write_text(
        '<p><img src="' + image_uri(raw.getvalue()) + '"/></p>', encoding="utf-8"
    )
    result = conversion(path)
    validation = (
        epubcheck(Path(result["output"]), output, jar, java)
        if result["status"] == "passed"
        else None
    )
    records.append(
        {
            "case": "EPUB WebP image without fallback",
            "id": "A03",
            "observed_issue": bool(validation and validation["exit"]),
            "input": str(path),
            "output_check": validation,
            "conversion": result,
        }
    )
    return records


def frozen_cases(output, executable):
    archive = CArchiveReader(str(executable))
    positive = verify_archive(archive)
    names = {n.replace("\\", "/"): n for n in archive.toc}
    inputs = json.loads(archive.extract(names["build-metadata/build-inputs.json"]))
    dll = next(
        f
        for f in inputs["files"]
        if f["group"] == "binary"
        and f["owner"] == "pymupdf"
        and f["target"].endswith(".dll")
    )

    def changed_dll(data):
        value = bytearray(data)
        value[len(value) // 2] ^= 1
        return bytes(value)

    wrapped = InjectedArchive(archive, dll["target"], changed_dll)
    try:
        result = verify_archive(wrapped)
        actual = next(f for f in result["archive_files"] if f["file"] == dll["target"])
        dll_result = {
            "accepted": True,
            "file": dll["target"],
            "source_sha256": dll["sha256"],
            "frozen_sha256": actual["sha256"],
        }
    except Exception as exc:
        dll_result = {"accepted": False, "error": str(exc)}
    records = [
        {
            "id": "A04",
            "case": "Changed native DLL bytes",
            "observed_issue": dll_result["accepted"],
            "result": dll_result,
        }
    ]
    altered_package = {}

    def changed_package(data):
        sbom = json.loads(data)
        package = next(p for p in sbom["packages"] if p["name"] == "pymupdf")
        altered_package["before"] = copy.deepcopy(package)
        package["versionInfo"] = "0.0.0-audit"
        package["licenseDeclared"] = "MIT"
        altered_package["after"] = copy.deepcopy(package)
        return json.dumps(sbom).encode("utf-8")

    try:
        verify_archive(
            InjectedArchive(archive, "build-metadata/sbom.spdx.json", changed_package)
        )
        accepted = True
    except Exception as exc:
        accepted = False
        altered_package["error"] = str(exc)
    records.append(
        {
            "id": "A05",
            "case": "Changed embedded package version/license",
            "observed_issue": accepted,
            "accepted": accepted,
            "package": altered_package,
        }
    )
    release = output / "sidecar-release"
    release.mkdir()
    for name in ("magic_converter.exe", "magic_converter-cli.exe"):
        os.link(executable.parent / name, release / name)
    (release / "sbom.spdx.json").write_bytes(b"invalid external SPDX JSON")
    (release / "build-inputs.json").write_bytes(b"invalid external inputs JSON")
    from tools.validation.verify_release import verify

    result = verify(release)
    (output / "sidecar-release-result.json").write_text(
        json.dumps(result, indent=2), encoding="utf-8"
    )
    records.append(
        {
            "id": "A06",
            "case": "Invalid distribution sidecars",
            "observed_issue": result["status"] == "passed",
            "release_status": result["status"],
            "report": str(output / "sidecar-release-result.json"),
        }
    )
    return records, {
        "archive_files": len(positive["archive_files"]),
        "exe_sha256": digest(executable.read_bytes()),
    }


def evidence_cases(output):
    from tools.validation import verify_repro_build as repro

    temporary_root = output / "stale-repro-root"
    report_dir = temporary_root / ".test-artifacts/reused"
    report_dir.mkdir(parents=True)
    old = report_dir / "result.json"
    old.write_text('{"status":"passed","previous_run":true}', encoding="utf-8")
    previous = digest(old.read_bytes())
    try:
        with patch.object(repro, "ROOT", temporary_root), patch.object(
            sys, "argv", ["verify_repro_build.py", "--output", str(report_dir)]
        ), patch.object(
            repro.subprocess, "check_output", side_effect=[b"missing-tracked-file.py\0", b""]
        ):
            code = repro.main()
    except Exception as exc:
        failure = {"type": type(exc).__name__, "error": str(exc)}
    else:
        failure = {"exit_code": code} if code else None
    unchanged = digest(old.read_bytes()) == previous
    records = [
        {
            "id": "A07",
            "case": "Previous passed reproduction report survives early failure",
            "observed_issue": bool(failure and unchanged),
            "new_error": failure,
            "report_unchanged": unchanged,
            "report": json.loads(old.read_text(encoding="utf-8")),
        }
    ]
    policy_root = output / "empty-public-evidence"
    (policy_root / "packaging").mkdir(parents=True)
    policy = {
        "public_release_approved": True,
        "obligations_reviewed": True,
        "project_license_file": "LICENSE",
        "corresponding_source_package": "source.zip",
    }
    (policy_root / "packaging/release-policy.json").write_text(
        json.dumps(policy), encoding="utf-8"
    )
    (policy_root / "LICENSE").write_bytes(b"")
    (policy_root / "source.zip").write_bytes(b"")
    try:
        public_gate(policy_root)
        accepted = True
    except Exception:
        accepted = False
    records.append(
        {
            "id": "A08",
            "case": "Empty public-release evidence",
            "observed_issue": accepted,
            "accepted": accepted,
            "evidence_bytes": {"LICENSE": 0, "source.zip": 0},
            "note": "Requires manual approval flags first; current policy remains false.",
        }
    )
    return records


def committed_source_case(output):
    """Check Git-exported bytes, not a byte-preserving worktree copy."""
    archive = output / "git-source.zip"
    subprocess.run(
        ["git", "archive", "--format=zip", "--output", str(archive), "HEAD"],
        cwd=ROOT,
        check=True,
    )

    def read(name):
        return json.loads((ROOT / name).read_text(encoding="utf-8"))

    notices = read("packaging/notices-lock.json")
    entries = [n for p in notices["packages"] for n in p["notices"]]
    entries += [
        {"file": p["metadata"], "sha256": p["metadata_sha256"]}
        for p in notices["packages"]
    ]
    entries += [
        {"file": notices["python"]["notice"], "sha256": notices["python"]["sha256"]}
    ]
    entries += [
        e for a in read("packaging/source-assets.json")["assets"] for e in a["files"]
    ]
    entries += [
        {
            "file": e["target"],
            "sha256": e["sha256"],
            "acquired_binary": Path(e["target"]).suffix in {".dll", ".exe"},
        }
        for a in read("packaging/vendor-lock.json")["artifacts"]
        for e in a["files"]
    ]
    entries += [
        {
            "file": "packaging/schemas/spdx-2.3.json",
            "sha256": read("packaging/build-environment.json")["spdx_schema_sha256"],
        }
    ]
    failures, checked, acquired = [], 0, []
    with zipfile.ZipFile(archive) as z:
        names = set(z.namelist())
        for entry in entries:
            if entry["file"] not in names:
                if entry.get("acquired_binary"):
                    acquired.append(entry["file"])
                else:
                    failures.append(
                        {"file": entry["file"], "error": "not in Git export"}
                    )
                continue
            checked += 1
            actual = digest(z.read(entry["file"]))
            if actual != entry["sha256"]:
                failures.append(
                    {
                        "file": entry["file"],
                        "expected": entry["sha256"],
                        "actual": actual,
                    }
                )
    return {
        "id": "A09",
        "case": "Committed source raw hashes",
        "observed_issue": bool(failures),
        "checked": checked,
        "downloaded_binaries": acquired,
        "failures": failures,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--jar",
        type=Path,
        default=ROOT / ".build-cache/validators/epubcheck-5.4.0/epubcheck.jar",
    )
    parser.add_argument("--java", default="java")
    parser.add_argument(
        "--executable", type=Path, default=ROOT / "dist" / ("v" + __version__) / "magic_converter-cli.exe"
    )
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    if not output.is_relative_to(ROOT / ".test-artifacts"):
        raise ValueError("Audit artifacts must stay within .test-artifacts")
    output.mkdir(parents=True, exist_ok=False)
    records = book_cases(output, args.jar.resolve(), args.java)
    frozen, control = frozen_cases(output, args.executable.resolve())
    records += frozen + evidence_cases(output) + [committed_source_case(output)]
    findings = sorted({r["id"] for r in records if r["observed_issue"]})
    result = {
        "status": "findings" if findings else "passed",
        "audit_completed": True,
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT)
        .decode()
        .strip(),
        "findings": findings,
        "control": control,
        "cases": records,
    }
    (output / "report.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "findings": findings,
                "report": str(output / "report.json"),
            },
            indent=2,
        )
    )
    return 1 if args.strict and findings else 0


if __name__ == "__main__":
    raise SystemExit(main())
