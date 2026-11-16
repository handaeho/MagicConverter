"""Build the Qt rendering helper against the same 6.12.0 DLLs as the Python baseline."""

import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile

import pefile

ROOT = Path(__file__).resolve().parents[2]


def digest(path):
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def imports(dll, staging, librarian):
    with pefile.PE(dll, fast_load=True, max_symbol_exports=65536) as image:
        image.parse_data_directories(directories=[0])
        names = []
        for item in image.DIRECTORY_ENTRY_EXPORT.symbols:
            if not item.name:
                continue
            section = image.get_section_by_rva(item.address)
            data = section and not item.forwarder and not section.Characteristics & 0x20000000
            names.append("  " + item.name.decode("ascii") + (" DATA" if data else ""))
        if len(names) != image.DIRECTORY_ENTRY_EXPORT.struct.NumberOfNames:
            raise ValueError(f"Truncated Qt exports in {dll.name}")
    definition = staging / f"{dll.stem}.def"
    definition.write_text(f"LIBRARY {dll.name}\nEXPORTS\n" + "\n".join(names) + "\n", encoding="ascii")
    library = staging / f"{dll.stem}.lib"
    subprocess.run([librarian, "/nologo", "/machine:x64", f"/def:{definition}", f"/out:{library}"], check=True)
    return library


def prepare(pyside, include, output, compiler, librarian):
    if output.exists():
        raise ValueError("Choose a new Qt runtime output directory.")
    pins = json.loads(Path(__file__).with_name("rust_qt_runtime.json").read_bytes())["pyside_files"]
    for name, value in pins.items():
        if digest(pyside / name) != value:
            raise ValueError(f"Use the pinned Qt 6.12.0 runtime. Unexpected bytes in {name}.")
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=output.parent) as temporary:
        stage = Path(temporary)
        runtime = stage / "runtime"
        runtime.mkdir()
        for name in pins:
            destination = runtime / name
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(pyside / name, destination)
        for name in ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
            shutil.copyfile(ROOT / "vendor/rhwp" / name, runtime / name)
        (runtime / "qt.conf").write_text("[Paths]\nPlugins=plugins\n", encoding="ascii")
        modules = ("QtCore", "QtGui", "QtSvg")
        libraries = [imports(pyside / f"{name.replace('Qt', 'Qt6', 1)}.dll", stage, librarian)
                     for name in modules]
        executable = runtime / "magic_qt_render.exe"
        source = ROOT / "rust/magic-converter/native/qt_render.cpp"
        subprocess.run([compiler, "/nologo", "/EHsc", "/std:c++20", "/utf-8", "/Zc:__cplusplus", "/O2", "/MD",
                        f"/I{include}", *(f"/I{include / module}" for module in modules),
                        f"/Fo{stage / 'qt_render.obj'}", f"/Fe{executable}", source, *libraries], check=True)
        runtime.rename(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pyside", type=Path, default=ROOT / ".venv/Lib/site-packages/PySide6")
    parser.add_argument("--include", type=Path, required=True, help="Qt 6.12.0 MSVC x64 SDK include directory")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--compiler", default="cl.exe")
    parser.add_argument("--librarian", default="lib.exe")
    args = parser.parse_args()
    prepare(args.pyside.resolve(), args.include.resolve(), args.output.resolve(), args.compiler, args.librarian)
