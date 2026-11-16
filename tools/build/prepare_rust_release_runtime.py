"""Use the previously published, source-backed Windows engine builds."""

import argparse
import hashlib
from io import BytesIO
import json
from pathlib import Path
from zipfile import ZipFile


RELEASE = "https://github.com/handaeho/MagicConverter/releases/download/v2.2.39/"
ASSETS = {
    "MagicConverter-native-engines.zip": "202c0f159ff8eb2c27861805c3d89fd9825828f01833d1e837648244195b922f",
    "MagicConverter-runtime-inputs.zip": "9c8fc90e192ce9d7b635d98438206d7a2d74d361d6243eae1b2ec2eecefd7390",
    "MagicConverter-native-corresponding-source.zip": "e4fcc4c00b0ba31af3512cc9d380d9e5619b51ed9d012c2f211bf78beb8f1de8",
}
REDIST = {
    "rhwp": ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"),
    "mupdf": ("msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll"),
    "qt": ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "vcruntime140.dll", "vcruntime140_1.dll"),
}


def digest(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def one_member(archive, prefix="", suffix=""):
    names = [name for name in archive.namelist() if name.startswith(prefix) and name.endswith(suffix)]
    if len(names) != 1:
        raise ValueError(f"Expected one archive member for {prefix!r}, {suffix!r}: {names}")
    return names[0]


def prepare(payload, assets):
    vendor = payload / "vendor"
    archives = {name: assets / name for name in ASSETS}
    for name, path in archives.items():
        if digest(path) != ASSETS[name]:
            raise ValueError(f"Incorrect v2.2.39 archive: {name}")
    replacements = {}
    with ZipFile(archives["MagicConverter-native-engines.zip"]) as engines:
        for name in ("ffmpeg.exe", "ffprobe.exe", "LICENSE", "README.txt"):
            path = f"ffmpeg/{name}"
            replacements[path] = engines.read(f"vendor/{path}")
        for folder, names in REDIST.items():
            for name in names:
                replacements[f"{folder}/{name}"] = engines.read(f"vendor/runtime/{name}")
        replacements["rhwp/runtime-licenses/LICENSE.TXT"] = engines.read("vendor/runtime/LICENSE.TXT")
    with ZipFile(archives["MagicConverter-runtime-inputs.zip"]) as inputs:
        for wheel, folder, names in (
            ("pyside6", "qt", ("Qt6Core.dll", "Qt6Gui.dll", "Qt6Svg.dll", "plugins/platforms/qoffscreen.dll")),
            ("pymupdf", "mupdf", ("mupdfcpp64.dll",)),
        ):
            with ZipFile(BytesIO(inputs.read(one_member(inputs, f"wheels/{wheel}-", ".whl")))) as archive:
                for name in names:
                    replacements[f"{folder}/{name}"] = archive.read(one_member(archive, suffix=f"/{name}"))
    with ZipFile(archives["MagicConverter-native-corresponding-source.zip"]) as sources:
        for name in ("qtbase-6.12.0.spdx", "qtsvg-6.12.0.spdx"):
            replacements[f"qt/licenses/{name}"] = sources.read(f"notices/qt/{name}")

    for name, data in replacements.items():
        path = vendor / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    for name in ("avcodec-63", "avdevice-63", "avfilter-12", "avformat-63", "avutil-61", "swresample-7", "swscale-10"):
        (vendor / "ffmpeg" / f"{name}.dll").unlink(missing_ok=True)
    for name in ("ffmpeg/rust_ffmpeg_runtime.json", "qt/rust_qt_runtime.json"):
        (vendor / name).unlink(missing_ok=True)
    record = {
        "upstream_release": RELEASE,
        "archives": ASSETS,
        "members": {name: hashlib.sha256(data).hexdigest() for name, data in sorted(replacements.items())},
    }
    (vendor / "published-runtime.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--payload", required=True, type=Path)
    parser.add_argument("--assets", required=True, type=Path)
    args = parser.parse_args()
    if not (args.payload / "magic_converter-cli.exe").is_file():
        parser.error("Supply the staged Rust Windows payload.")
    prepare(args.payload, args.assets)
