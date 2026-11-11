"""Build the Windows installer and the source needed to rebuild it."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
from xml.etree import ElementTree as ET
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from windows_runtime_bundle import write_pinned_archive, write_runtime_archive


ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.dependencies import fetch_vendor

ENGINE_FILES = frozenset("""
vendor/ffmpeg/ffmpeg.exe vendor/ffmpeg/ffprobe.exe vendor/ffmpeg/LICENSE vendor/ffmpeg/README.txt
vendor/rhwp/rhwp.exe vendor/rhwp/LICENSE vendor/rhwp/THIRD_PARTY_LICENSES.md
vendor/rhwp/msvcp140.dll vendor/rhwp/vcruntime140.dll vendor/rhwp/vcruntime140_1.dll
vendor/rhwp/runtime-licenses/LICENSE.TXT vendor/runtime/LICENSE.TXT
vendor/runtime/concrt140.dll vendor/runtime/msvcp140.dll vendor/runtime/msvcp140_1.dll
vendor/runtime/msvcp140_2.dll vendor/runtime/msvcp140_atomic_wait.dll vendor/runtime/msvcp140_codecvt_ids.dll
vendor/runtime/vccorlib140.dll vendor/runtime/vcruntime140.dll vendor/runtime/vcruntime140_1.dll
vendor/runtime/vcruntime140_threads.dll
""".split())
RUNTIME_PACKAGES = set("""
beautifulsoup4 brotli cffi charset-normalizer cryptography defusedxml et-xmlfile
fonttools lxml markdown olefile openpyxl pdfminer-six pdfplumber pillow pillow-heif
pycparser pymupdf pypdf pypdfium2 pyside6 python-docx python-pptx
pywin32 pyyaml rarfile shiboken6 soupsieve striprtf tomli-w typing-extensions
xlrd xlsxwriter zopfli
""".split())
APP_DATA = """
LICENSE assets/magic_converter-tool.ico licenses/Filee-MIT.txt licenses/Microsoft-VC-Runtime-2026.txt run_magic_converter.py
packaging/public-windows/THIRD_PARTY_NOTICES.txt
magic_converter/data/hwp/README.txt magic_converter/data/hwp/table_picture.hwp
magic_converter/data/hwpx/LICENSE magic_converter/data/hwpx/NOTICE
magic_converter/data/hwpx/SOURCE.txt magic_converter/data/hwpx/Skeleton.hwpx
""".split()
SOURCE_INPUTS = """
assets/README.md assets/magic_converter-tool.svg docs/user/PUBLIC_WINDOWS.md docs/user/UNINSTALL_WINDOWS.md
docs/release/WINDOWS_INSTALLER_SOURCE.md
packaging/app.manifest packaging/public-windows/launcher.cs requirements/windows-py314.lock
packaging/public-windows/runtime-bootstrap.json
packaging/public-windows/runtime-requirements.txt
packaging/public-windows/engines-lock.json tools/__init__.py tools/dependencies/__init__.py tools/dependencies/fetch_vendor.py
packaging/public-windows/setup.cs packaging/public-windows/process_arguments.cs
packaging/public-windows/install_location.cs packaging/public-windows/setup_wizard.cs
packaging/public-windows/uninstall.cs packaging/public-windows/uninstall-files.txt
tests/documents/test_hwp_objects.py tools/build/generate_icon.py
tools/build/build_public_windows.py tools/build/build_windows_uninstaller.py
tools/build/windows_runtime_bundle.py
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
    return ("# Windows x64, CPython 3.14\n" + "\n".join(sorted(selected.values())) + "\n").encode()


def compile_exe(compiler, temp, output, entry, target, *options):
    subprocess.run([
        str(compiler), "/nologo", "/warnaserror+", "/optimize+", "/platform:x64", "/codepage:65001",
        "/target:" + target, "/out:" + str(output), *options,
        "/win32manifest:" + str(ROOT / "packaging/app.manifest"),
        "/win32icon:" + str(ROOT / "assets/magic_converter-tool.ico"),
        "/reference:System.Windows.Forms.dll",
        str(ROOT / "packaging/public-windows" / entry),
        str(ROOT / "packaging/public-windows/process_arguments.cs"), "version.cs",
    ], cwd=temp, check=True)


def build(output, runtime_dir=None, vendor_cache=None):
    config = files(SOURCE_INPUTS)
    license_manifest = json.loads((ROOT / "licenses/stage7/manifest.json").read_text(encoding="utf-8"))
    stage7 = ["licenses/stage7/manifest.json", *("licenses/stage7/" + item["file"] for item in license_manifest)]
    app = files([*APP_DATA, *stage7, *(p.relative_to(ROOT).as_posix() for p in (ROOT / "magic_converter").rglob("*.py"))])
    for item in license_manifest:
        if sha(app["licenses/stage7/" + item["file"]]) != item["sha256"]:
            raise ValueError("Changed stage-7 license: " + item["file"])
    version, = re.findall(r'^__version__ = "([0-9]+\.[0-9]+\.[0-9]+)"\r?$', app["magic_converter/__init__.py"].decode(), re.M)
    identity = ET.fromstring(config["packaging/app.manifest"]).find(
        "{urn:schemas-microsoft-com:asm.v1}assemblyIdentity"
    )
    if identity is None or identity.get("version") != version + ".0":
        raise ValueError("Windows manifest and app versions differ")
    lock = runtime_lock(config["packaging/public-windows/runtime-requirements.txt"])
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
        (temp / "version.cs").write_text(
            "using System.Reflection;\n"
            '[assembly: AssemblyTitle("MagicConverter")]\n'
            f'[assembly: AssemblyVersion("{version}.0")]\n'
            f'[assembly: AssemblyFileVersion("{version}.0")]\n',
            encoding="ascii",
        )
        compile_exe(compiler, temp, temp / "magic_converter.exe", "launcher.cs", "winexe", "/define:GUI")
        compile_exe(compiler, temp, temp / "magic_converter-cli.exe", "launcher.cs", "exe")
        setup_files = {
            **{"app/" + name: data for name, data in app.items()},
            "magic_converter.exe": (temp / "magic_converter.exe").read_bytes(),
            "magic_converter-cli.exe": (temp / "magic_converter-cli.exe").read_bytes(),
            "magic_converter-runtime.txt": lock,
        }
        payload = zip_bytes(setup_files)
        (temp / "setup.zip").write_bytes(payload)
        bootstrap = json.loads(config["packaging/public-windows/runtime-bootstrap.json"])
        runtime_digest, runtime_files = write_runtime_archive(temp / "runtime.zip", bootstrap, lock, runtime_dir)
        vendor = temp / "engines"
        vendor_manifest = json.loads(config["packaging/public-windows/engines-lock.json"])
        entries = fetch_vendor.targets(vendor, vendor_manifest)
        if entries.keys() != ENGINE_FILES:
            raise ValueError("Installer engine inputs must match the FFmpeg and rhwp allowlist")
        fetch_vendor.acquire(vendor, vendor_cache, offline=vendor_cache is not None, manifest=vendor_manifest)
        engine_digest, engine_files = write_pinned_archive(
            temp / "engines.zip", {name: (vendor / name, entry["sha256"]) for name, entry in entries.items()},
            ZIP_DEFLATED,
        )
        (temp / "runtime.cs").write_text(
            "internal static class RuntimeInfo {\n"
            f'    internal const string PythonZip = "{bootstrap["python"]["filename"]}";\n'
            f'    internal const string PipWheel = "{bootstrap["pip"]["filename"]}";\n'
            f'    internal const string PythonVersion = "{bootstrap["python_version"]}";\n'
            f'    internal const string ArchiveSha256 = "{runtime_digest}";\n'
            f'    internal const string EnginesSha256 = "{engine_digest}";\n'
            "}\n", encoding="ascii",
        )
        setup = ready / "MagicConverter-Setup.exe"
        compile_exe(
            compiler, temp, setup, "setup.cs", "winexe",
            "/reference:System.IO.Compression.dll", "/reference:System.IO.Compression.FileSystem.dll",
            "/reference:System.Drawing.dll", "/reference:Microsoft.CSharp.dll",
            "/resource:setup.zip,magic-converter-setup.zip",
            "/resource:runtime.zip,magic-converter-runtime.zip", "runtime.cs",
            "/resource:engines.zip,magic-converter-engines.zip",
            str(ROOT / "packaging/public-windows/install_location.cs"),
            str(ROOT / "packaging/public-windows/setup_wizard.cs"),
        )
        if setup.read_bytes().count(payload) != 1:
            raise ValueError("Setup does not contain the exact generated application archive")
        (ready / "THIRD_PARTY_NOTICES.txt").write_bytes(app["packaging/public-windows/THIRD_PARTY_NOTICES.txt"])
        (ready / "MagicConverter-corresponding-source.zip").write_bytes(source_zip)
        manifest = {
            "project_version": version,
            "profile": "Windows x64 installer with embedded Python, runtime wheels, FFmpeg and rhwp",
            "compiler_sha256": sha(compiler.read_bytes()),
            "setup_archive_sha256": sha(payload),
            "setup_files": {name: sha(data) for name, data in setup_files.items()},
            "runtime_archive_sha256": runtime_digest,
            "runtime_files": runtime_files,
            "engine_archive_sha256": engine_digest,
            "engine_files": engine_files,
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
    parser.add_argument("--runtime-dir", type=Path, help="Use local copies of the pinned publisher inputs")
    parser.add_argument("--vendor-cache", type=Path, help="Use the pinned local FFmpeg and rhwp archives")
    options = parser.parse_args()
    build(options.output, options.runtime_dir, options.vendor_cache)
