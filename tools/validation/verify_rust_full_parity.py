"""Compare the pinned Python CLI and standalone Rust executable on shared fixtures."""

import argparse
import bz2
from concurrent.futures import ThreadPoolExecutor
from compression import zstd
import gzip
import hashlib
import io
import json
import lzma
import os
from pathlib import Path
import stat
import struct
import subprocess
import sys
import tarfile
import time
import warnings
import zipfile


def digest(path):
    with path.open("rb") as file:
        return hashlib.file_digest(file, "sha256").hexdigest()


def run(prefix, arguments, *, environment=None):
    process = subprocess.run([*prefix, *map(str, arguments)], capture_output=True, env=environment,
                             timeout=180, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    try:
        return process.returncode, json.loads(process.stdout)
    except ValueError as error:
        raise RuntimeError(process.stderr.decode("utf-8", errors="replace")) from error


def native_environment():
    environment = os.environ.copy()
    environment["PATH"] = str(Path(environment.get("SYSTEMROOT", "C:/Windows")) / "System32")
    for key in ("MAGIC_CONVERTER_PROFILE_DIR", "MAGIC_CONVERTER_FFMPEG", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME",
                "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        environment.pop(key, None)
    return environment


def compare_plans(worker, matrix, root):
    output = root / "rust-plans.jsonl"
    with matrix.open("rb") as source, output.open("wb") as target:
        subprocess.run([worker, "compare-plans"], stdin=source, stdout=target,
                       check=True, timeout=30, env=native_environment())
    count = 0
    profiles = {}
    canonical = {"reference_routes": 0, "native_routes": 0, "missing_by_action": {}}
    with matrix.open("rb") as expected, output.open("rb") as actual:
        for a, b in zip(expected, actual, strict=True):
            a, b = json.loads(a), json.loads(b)
            native = b.pop("native")
            assert {"route": a["route"], "reason": a["reason"]} == b, (a, b)
            if a["profile"] == "all" and a["path"].startswith("input.") and a["route"] and a["route"]["steps"]:
                canonical["reference_routes"] += 1
                canonical["native_routes"] += int(native)
                if not native:
                    missing = canonical["missing_by_action"]
                    missing[a["action"]] = missing.get(a["action"], 0) + 1
            count += 1
            profiles[a["profile"]] = profiles.get(a["profile"], 0) + 1
    return {"comparisons": count, "profiles": profiles, "canonical": canonical}


def normalize(report, output):
    def visit(value):
        if isinstance(value, dict):
            return {key: visit(item) for key, item in value.items()}
        if isinstance(value, list):
            return [visit(item) for item in value]
        if isinstance(value, str):
            return value.replace(str(output), "<OUTPUT>")
        return value
    return visit(report)


def compare_cli(baseline, worker, root, evidence, *, subtitles=False):
    python = [sys.executable, "-B", baseline / "run_magic_converter.py"]
    rust = [worker]
    environment = native_environment()
    profile = root / "profile"
    formats = None
    for command in ("formats", "actions", "presets"):
        args = (command, "--data-dir", profile, "--json")
        expected = run(python, args)
        assert expected == run(rust, args, environment=environment), command
        assert expected[0] == 0
        if command == "formats":
            formats = expected[1]["formats"]
        evidence[command] = len(expected[1][command])
    assert formats

    def compare(label, inputs, selector, *, profiles=None, status=0):
        folder = root / label
        folder.mkdir()
        reports = []
        for index, (prefix, env) in enumerate(((python, None), (rust, environment))):
            output = folder / ("python" if index == 0 else "rust")
            profile_dir = profiles[index] if profiles else profile
            receipt = folder / f"{index}.json"
            command = ("convert", *inputs, *selector, "--output", output, "--data-dir", profile_dir, "--report", receipt, "--json")
            code, data = run(prefix, command, environment=env)
            assert code == status, (label, prefix, code, data)
            assert json.loads(receipt.read_bytes()) == data
            files = [Path(name) for job in data.get("jobs", []) for name in job["outputs"]]
            reports.append((normalize(data, output), {file.name: digest(file) for file in files}))
            assert not list(output.glob(".magic_converter-*"))
        assert reports[0] == reports[1], label
        evidence[label] = {"inputs": len(inputs), "outputs": reports[0][1], "status": status}
        return folder

    fixtures = root / "fixtures"
    fixtures.mkdir()
    inputs = []
    for fmt in formats:
        if fmt["readable"] and fmt["id"] != "rtfd":
            for extension in fmt["extensions"]:
                file = fixtures / f"한글 🪄 {len(inputs):02d}.{extension.upper()}"
                file.write_bytes(bytes(range(256)) if inputs else b"")
                inputs.append(file)
    fingerprints = {file: digest(file) for file in inputs}
    copy = ("--action", "tool:copy_original")
    compare("copy-all-readable-aliases", inputs + inputs[:2], copy)
    pdf = next(file for file in inputs if file.suffix == ".PDF")
    compare("pdf-identity", [pdf], ("--to", "pdf"))
    compare("missing-file", [fixtures / "absent.txt"], copy, status=1)
    compare("unsupported-input", [fixtures / "file.unregistered"], copy, status=1)
    compare("invalid-action", [pdf], ("--action", "unregistered"), status=1)

    settings = json.loads((profile / "settings.json").read_bytes())
    settings["presets"].append({"id": "sample", "name": "기본/보존", "action_key": "tool:copy_original",
                                "output": {"template": "{{복사}}_{preset}_{stem}", "collision": "rename"}})
    settings["presets"].append({"id": "shift-test", "name": "자막 앞당김", "action_key": "tool:subtitle_shift",
                                "tools": {"subtitle_offset_ms": -1000}})
    profiles = (root / "python-profile", root / "rust-profile")
    for directory in profiles:
        directory.mkdir()
        (directory / "settings.json").write_text(json.dumps(settings, ensure_ascii=False), encoding="utf-8")
    compound = next(file for file in inputs if file.name.endswith(".TAR.GZ"))
    compare("custom-preset", [compound], ("--preset", "sample"), profiles=profiles)
    if subtitles:
        srt = fixtures / "한국어 cp949.srt"
        srt.write_bytes(("1\r\n00:00:00,100 --> 00:00:00,800\r\n제외\r\n\r\n"
                         "2\r\n00:00:00,900 --> 00:00:02,000\r\n본문 &amp; 문자\r\n둘째 줄\r\n").encode("cp949"))
        vtt = fixtures / "utf16 자막.vtt"
        vtt.write_bytes(("WEBVTT\n\nSTYLE\n::cue { color: lime; }\n\nNOTE 설명\n노트\n\n"
                         "cue-id\n00:00:00.900 --> 00:00:02.000 align:start\n"
                         "<00:00:01.200><b>내용</b> &amp; text\n두 줄\n").encode("utf-16"))
        for target in ("srt", "vtt", "txt"):
            compare(f"subtitles-{target}", [srt, vtt], ("--action", f"subtitle:{target}"))
        compare("subtitles-shift", [srt, vtt], ("--preset", "shift-test"), profiles=profiles)
        corrupt = fixtures / "invalid.srt"
        corrupt.write_text("00:99:00,000 --> 00:00:02,000\ninvalid", encoding="utf-8")
        compare("subtitles-invalid-time", [corrupt], ("--action", "subtitle:vtt"), status=1)

    concurrent = root / "concurrent"
    concurrent.mkdir()
    expected = digest(compound)
    for prefix, env in ((python, None), (rust, environment)):
        output = concurrent / ("python" if env is None else "rust")
        output.mkdir()
        occupied = output / (compound.name[:-7] + "_copy.TAR.GZ")
        occupied.write_bytes(b"existing user file")
        with ThreadPoolExecutor(max_workers=3) as pool:
            futures = [pool.submit(run, prefix, ("convert", compound, *copy, "--output", output, "--json",
                       "--data-dir", concurrent / f"profile-{index}"), environment=env) for index in range(3)]
            results = [future.result() for future in futures]
        published = [Path(result["jobs"][0]["outputs"][0]) for status, result in results if status == 0]
        assert len(set(published)) == 3 and all(digest(file) == expected for file in published)
        assert occupied.read_bytes() == b"existing user file"
        assert not list(output.glob(".magic_converter-*"))
    assert all(digest(file) == sha for file, sha in fingerprints.items())
    evidence["concurrent_copy"] = {"outputs": 3, "existing_and_source_preserved": True}


def archive_fixtures(root, *, extended=False):
    root.mkdir()
    members = [
        ("한글/", None, 0o751),
        ("한글/빈 파일.txt", b"", 0o640),
        ("한글/사진.bin", bytes(range(256)) * 24, 0o600),
        ("empty/", None, 0o700),
    ]
    timestamp = 1_700_000_001
    ticks = 116_444_736_000_000_000 + timestamp * 10_000_000 + 1_234_567
    unix = struct.pack("<HHBi", 0x5455, 5, 1, timestamp)
    ntfs = struct.pack("<HHIHHQQQ", 0x000A, 32, 0, 1, 24, ticks, 0, 0)
    paths = {}
    zip_methods = {"zip": zipfile.ZIP_DEFLATED}
    if extended:
        zip_methods.update({"zip-bzip2": zipfile.ZIP_BZIP2, "zip-lzma": zipfile.ZIP_LZMA,
                            "zip-zstd": zipfile.ZIP_ZSTANDARD})
    for identifier, codec in zip_methods.items():
        zipped = paths[identifier] = root / f"{identifier}.zip"
        with zipfile.ZipFile(zipped, "w") as archive:
            for name, data, mode in members:
                info = zipfile.ZipInfo(name)
                info.create_system = 3
                info.external_attr = ((stat.S_IFDIR if data is None else stat.S_IFREG) | mode) << 16
                info.extra = unix + (ntfs if data else b"")
                archive.writestr(info, data or b"", compress_type=codec if data else zipfile.ZIP_STORED)
    for suffix, mode in (("tar", "w"), ("tar.gz", "w:gz")):
        paths[suffix] = root / f"members.{suffix}"
        with tarfile.open(paths[suffix], mode, format=tarfile.PAX_FORMAT) as archive:
            for name, data, permissions in members:
                info = tarfile.TarInfo(name.rstrip("/"))
                info.mode = permissions
                info.mtime = timestamp
                if data:
                    info.pax_headers["mtime"] = "1700000001.123456700"
                if data is None:
                    info.type = tarfile.DIRTYPE
                    archive.addfile(info)
                else:
                    info.size = len(data)
                    archive.addfile(info, io.BytesIO(data))
    if extended:
        tar = paths["tar"].read_bytes()
        for suffix, compress in (("tar.xz", lzma.compress), ("tar.bz2", bz2.compress), ("tar.zst", zstd.compress)):
            paths[suffix] = root / f"members.{suffix}"
            paths[suffix].write_bytes(compress(tar))
    single = paths["gz"] = root / "renamed.gz"
    with single.open("wb") as file:
        for name, modified, payload in (("café.txt", timestamp, b"first"), ("second", timestamp + 1, b"second")):
            with gzip.GzipFile(filename=name, fileobj=file, mode="wb", mtime=modified) as archive:
                archive.write(payload)
    return paths


def compare_archives(baseline, worker, root, *, extended=False):
    sys.path.insert(0, str(baseline))
    from magic_converter.core.archives.archive_converter import archive_members

    def members(path):
        metadata = {}
        signature = []
        with archive_members(path, metadata=metadata) as entries:
            for name, size, data in entries:
                signature.append({"name": name, "size": size,
                                  "sha256": hashlib.file_digest(data, "sha256").hexdigest() if data is not None else None,
                                  "mtime_ns": metadata[name][0], "mode": metadata[name][1]})
        return signature

    def tree(path, explicit, *, timestamps):
        return [{"name": file.relative_to(path).as_posix(), "type": "folder" if file.is_dir() else "file",
                 "sha256": None if file.is_dir() else digest(file),
                 "mtime_ns": file.stat().st_mtime_ns if timestamps and file.relative_to(path).as_posix() in explicit else None}
                for file in sorted(path.rglob("*"))]

    root.mkdir()
    profile = root / "profile"
    python = [sys.executable, "-B", baseline / "run_magic_converter.py"]
    rust = [worker]
    environment = native_environment()
    tar_exe = Path(environment["PATH"]) / "tar.exe"
    pairs = ((python, None, "python"), (rust, environment, "rust"))
    inputs = archive_fixtures(root / "fixtures", extended=extended)
    receipts = []
    transient = []

    def convert(prefix, env, source, action, destination, *, expected_ok=True):
        command = ("convert", source, "--action", action, "--output", destination, "--data-dir", profile, "--json")
        for attempt in range(3):
            code, report = run(prefix, command, environment=env)
            jobs = report.get("jobs", [])
            errors = [error for job in jobs for error in job["errors"]]
            if (not expected_ok or code != 1 or not errors
                    or not all(any(f"[WinError {number}]" in error for number in (5, 32, 33)) for error in errors)):
                break
            assert not any(job["outputs"] for job in jobs)
            assert not (destination.exists() and any(destination.iterdir()))
            transient.append({"input": source.name, "action": action, "error": errors})
            if attempt < 2:
                time.sleep(0.15)
        assert code == (0 if expected_ok else 1), (action, source, prefix, report)
        assert not list(destination.glob(".magic_converter-*")), destination
        paths = [Path(name) for job in report.get("jobs", []) for name in job["outputs"]]
        assert len(paths) == int(expected_ok), (action, report)
        return (paths[0] if paths else None), normalize(report, destination)

    for input_id, source in inputs.items():
        source_hash = digest(source)
        explicit = {row["name"] for row in members(source) if row["mtime_ns"] is not None}
        for action in ("archive:zip", "archive:tar", "archive:gzip", "tool:archive_extract"):
            converted = [convert(prefix, env, source, action, root / input_id / action.replace(":", "-") / label)
                         for prefix, env, label in pairs]
            (py_out, py_report), (rust_out, rust_report) = converted
            assert py_report == rust_report, (input_id, action, py_report, rust_report)
            if action == "tool:archive_extract":
                assert tree(py_out, explicit, timestamps=True) == tree(rust_out, explicit, timestamps=True), input_id
                receipts.append({"source": input_id, "action": action, "members": tree(rust_out, explicit, timestamps=True)})
                continue
            signature = members(py_out)
            assert signature == members(rust_out), (input_id, action, signature, members(rust_out))
            for target in (py_out, rust_out):
                windows = target.parent / "bsdtar"
                windows.mkdir()
                subprocess.run([tar_exe, "-xf", target, "-C", windows], check=True, capture_output=True)
            assert tree(py_out.parent / "bsdtar", explicit, timestamps=False) == tree(rust_out.parent / "bsdtar", explicit, timestamps=False)
            py_from_rust, _ = convert(python, None, rust_out, "tool:archive_extract", py_out.parent / "cross")
            rust_from_py, _ = convert(rust, environment, py_out, "tool:archive_extract", rust_out.parent / "cross")
            assert tree(py_from_rust, explicit, timestamps=True) == tree(rust_from_py, explicit, timestamps=True)
            receipts.append({"source": input_id, "action": action, "members": signature,
                             "cross_read": True, "python_bytes": py_out.stat().st_size, "rust_bytes": rust_out.stat().st_size})
        assert digest(source) == source_hash

    rejections = {}
    for label, names in (("parent-component", ("../outside.txt",)), ("same-name", ("duplicate.txt", "duplicate.txt")),
                         ("case-alias", ("Name.txt", "name.txt")), ("parent-case-alias", ("Folder/a", "folder/b")),
                         ("reserved-name", ("CON.txt",)), ("bad-crc", ("a",)), ("link", ("link",))):
        source = root / "fixtures" / (label + ".zip")
        payload = b"unique fixture"
        with warnings.catch_warnings(), zipfile.ZipFile(source, "w") as archive:
            warnings.simplefilter("ignore", UserWarning)
            for name in names:
                info = zipfile.ZipInfo(name)
                if label == "link":
                    info.create_system = 3
                    info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, payload)
        if label == "bad-crc":
            source.write_bytes(source.read_bytes().replace(payload, b"broken fixture", 1))
        before = digest(source)
        for prefix, env, edition in pairs:
            destination = root / "rejections" / label / edition
            convert(prefix, env, source, "tool:archive_extract", destination, expected_ok=False)
            assert (not destination.exists() or not list(destination.iterdir())) and digest(source) == before, (label, edition)
        rejections[label] = "both"
    emoji = root / "fixtures" / "emoji.zip"
    with zipfile.ZipFile(emoji, "w") as archive:
        archive.writestr("한글/사진🪄.txt", b"payload")
    emoji_signatures, windows_emoji = [], {}
    for prefix, env, edition in pairs:
        archive, _ = convert(prefix, env, emoji, "archive:zip", root / "emoji" / edition)
        emoji_signatures.append(members(archive))
        destination = archive.parent / "native"
        destination.mkdir()
        result = subprocess.run([tar_exe, "-xf", archive, "-C", destination], capture_output=True)
        windows_emoji[edition] = {"status": result.returncode,
                                  "stderr": result.stderr.decode("utf-8", errors="replace"),
                                  "files": tree(destination, set(), timestamps=False)}
    assert emoji_signatures[0] == emoji_signatures[1]
    original, = members(emoji)
    assert all(emoji_signatures[0][0][key] == original[key] for key in ("name", "size", "sha256", "mtime_ns"))
    assert windows_emoji["python"] == windows_emoji["rust"]
    return {"routes": receipts, "rejections": rejections, "windows_emoji_zip": windows_emoji,
            "transient_host_failures": transient}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--archives", action="store_true")
    parser.add_argument("--extended-archives", action="store_true")
    parser.add_argument("--subtitles", action="store_true")
    args = parser.parse_args()
    if os.name != "nt" or sys.flags.optimize:
        parser.error("Use Windows with assertions enabled")
    root = args.output.resolve()
    root.mkdir(parents=True, exist_ok=False)
    worker = args.worker.resolve(strict=True)
    evidence = {"status": "running", "worker_sha256": digest(worker), "cli": {}}
    try:
        evidence["planning"] = compare_plans(worker, args.matrix.resolve(strict=True), root)
        compare_cli(args.baseline.resolve(strict=True), worker, root, evidence["cli"], subtitles=args.subtitles)
        if args.archives or args.extended_archives:
            evidence["archives"] = compare_archives(args.baseline.resolve(strict=True), worker, root / "archives",
                                                     extended=args.extended_archives)
        evidence["status"] = "passed"
    except Exception as error:
        evidence.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
