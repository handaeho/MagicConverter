"""Build Windows launchers that distribute MagicConverter source and data only."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo


ROOT = Path(__file__).resolve().parents[2]
RUNTIME_PACKAGES = set("""
beautifulsoup4 brotli cffi charset-normalizer cryptography defusedxml et-xmlfile
fonttools lxml markdown olefile openpyxl pdfminer-six pdfplumber pillow pillow-heif
pycparser pymupdf pypdf pypdfium2 pyside6-essentials python-docx python-pptx
pywin32 pyyaml rarfile shiboken6 soupsieve striprtf tomli-w typing-extensions
xlrd xlsxwriter zopfli
""".split())
APP_DATA = """
LICENSE assets/magic_converter-tool.ico licenses/Filee-MIT.txt run_magic_converter.py
packaging/public-windows/THIRD_PARTY_NOTICES.txt
magic_converter/data/hwp/README.txt magic_converter/data/hwp/table_picture.hwp
magic_converter/data/hwpx/LICENSE magic_converter/data/hwpx/NOTICE
magic_converter/data/hwpx/SOURCE.txt magic_converter/data/hwpx/Skeleton.hwpx
""".split()
SOURCE_INPUTS = """
assets/README.md assets/magic_converter-tool.svg docs/user/PUBLIC_WINDOWS.md
packaging/app.manifest packaging/public-windows/launcher.cs requirements/windows-py314.lock
tests/documents/test_hwp_objects.py tools/build/generate_icon.py
tools/build/build_public_windows.py
""".split()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def files(names):
    result = {}
    for name in sorted(set(names)):
        path = ROOT / name
        if path.is_symlink() or not path.resolve().is_relative_to(ROOT):
            raise ValueError("Build input escapes the source tree: " + name)
        data = path.read_bytes()
        if data.startswith(b"MZ") or name.lower().endswith((".exe", ".dll", ".pyd", ".whl")):
            raise ValueError("A third-party binary cannot be a public build input: " + name)
        result[name] = data
    return result


def zip_bytes(contents):
    buffer = io.BytesIO()
    with ZipFile(buffer, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name, data in sorted(contents.items()):
            item = ZipInfo(name)
            item.external_attr = 0o100644 << 16
            archive.writestr(item, data, compress_type=ZIP_DEFLATED, compresslevel=9)
    return buffer.getvalue()


def runtime_lock(inputs):
    selected = {}
    for line in inputs.decode("utf-8").splitlines():
        package = line.partition("==")[0]
        if package in RUNTIME_PACKAGES:
            if package in selected or not re.fullmatch(r"[^ =]+==[^ ]+ --hash=sha256:[0-9a-f]{64}", line):
                raise ValueError("Invalid runtime wheel pin: " + package)
            selected[package] = line
    if selected.keys() != RUNTIME_PACKAGES:
        raise ValueError("Missing locked runtime wheels: " + ", ".join(sorted(RUNTIME_PACKAGES - selected.keys())))
    return ("# Windows x64, Python 3.14.7\n" + "\n".join(sorted(selected.values())) + "\n").encode()


def build(output):
    config = files(SOURCE_INPUTS)
    license_manifest = json.loads((ROOT / "licenses/stage7/manifest.json").read_text(encoding="utf-8"))
    stage7 = ["licenses/stage7/manifest.json", *("licenses/stage7/" + item["file"] for item in license_manifest)]
    app = files([*APP_DATA, *stage7, *(p.relative_to(ROOT).as_posix() for p in (ROOT / "magic_converter").rglob("*.py"))])
    for item in license_manifest:
        if sha(app["licenses/stage7/" + item["file"]]) != item["sha256"]:
            raise ValueError("Changed stage-7 license: " + item["file"])
    version, = re.findall(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"\r?$', app["magic_converter/__init__.py"].decode(), re.M)
    payload = zip_bytes(app)
    lock = runtime_lock(config["requirements/windows-py314.lock"])
    source = {**app, **config, "magic_converter-runtime.txt": lock}
    source_zip = zip_bytes(source)
    with ZipFile(io.BytesIO(source_zip)) as archive:
        if archive.namelist() != sorted(source) or any(archive.read(name) != data for name, data in source.items()):
            raise ValueError("Source archive does not match the build inputs")

    framework = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319"
    compiler = framework / "csc.exe"
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Choose an unused output directory: " + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="magic-public-", dir=output.parent) as work:
        temp = Path(work)
        ready = temp / "release"
        ready.mkdir()
        (temp / "app.zip").write_bytes(payload)
        (temp / "version.cs").write_text(
            "using System.Reflection;\n"
            '[assembly: AssemblyTitle("MagicConverter")]\n'
            f'[assembly: AssemblyVersion("{version}.0")]\n'
            f'[assembly: AssemblyFileVersion("{version}.0")]\n',
            encoding="ascii",
        )
        for target, name in (("winexe", "magic_converter.exe"), ("exe", "magic_converter-cli.exe")):
            flags = ["/define:GUI"] if target == "winexe" else []
            path = ready / name
            subprocess.run([
                str(compiler), "/nologo", "/optimize+", "/platform:x64", "/codepage:65001",
                "/target:" + target, "/out:" + str(path), *flags,
                "/win32manifest:" + str(ROOT / "packaging/app.manifest"),
                "/win32icon:" + str(ROOT / "assets/magic_converter-tool.ico"),
                "/reference:System.IO.Compression.dll", "/reference:System.IO.Compression.FileSystem.dll",
                "/reference:System.Windows.Forms.dll", "/resource:app.zip,magic-converter.zip",
                str(ROOT / "packaging/public-windows/launcher.cs"), "version.cs",
            ], cwd=temp, check=True)
            if path.read_bytes().count(payload) != 1:
                raise ValueError("EXE does not contain the exact generated application archive: " + name)
        (ready / "magic_converter-runtime.txt").write_bytes(lock)
        (ready / "THIRD_PARTY_NOTICES.txt").write_bytes(app["packaging/public-windows/THIRD_PARTY_NOTICES.txt"])
        (ready / "MagicConverter-corresponding-source.zip").write_bytes(source_zip)
        manifest = {
            "project_version": version,
            "profile": "Windows x64 launcher with separately installed runtime",
            "compiler_sha256": sha(compiler.read_bytes()),
            "app_archive_sha256": sha(payload),
            "source_archive_sha256": sha(source_zip),
            "app_files": {name: sha(data) for name, data in app.items()},
            "source_files": {name: sha(data) for name, data in source.items()},
            "runtime_packages": sorted(RUNTIME_PACKAGES),
        }
        (ready / "source-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        (ready / "SHA256SUMS.txt").write_text("".join(
            f"{sha(path.read_bytes())}  {path.name}\n" for path in sorted(ready.iterdir())
        ), encoding="ascii")
        ready.rename(output)
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    build(parser.parse_args().output)
