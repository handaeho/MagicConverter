# -*- mode: python ; coding: utf-8 -*-
"""Windows one-file build through the root build.ps1 entry point."""

from pathlib import Path
import sys
import hashlib
import json
from xml.etree import ElementTree as ET
from importlib.metadata import version
from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, collect_submodules
from PyInstaller.utils.win32 import versioninfo
from PyInstaller.config import CONF
root = Path(SPEC).resolve().parents[1]
sys.path.insert(0, str(root))
from magic_converter import __version__
from tools.build.build_inventory import validate_inputs, create as create_inventory

validate_inputs(root)
release_parts = tuple(map(int, __version__.split("."))) + (0,)
resource = versioninfo.load_version_info_from_text_file(root / "packaging" / "version_info.txt")
numeric_version = (release_parts[0] << 16 | release_parts[1], release_parts[2] << 16 | release_parts[3])
strings = {item.name: item.val for group in resource.kids for table in getattr(group, "kids", [])
           for item in getattr(table, "kids", []) if hasattr(item, "name")}
manifest = ET.parse(root / "packaging" / "app.manifest")
identity = manifest.find("{urn:schemas-microsoft-com:asm.v1}assemblyIdentity")
if ((resource.ffi.fileVersionMS, resource.ffi.fileVersionLS) != numeric_version
        or (resource.ffi.productVersionMS, resource.ffi.productVersionLS) != numeric_version
        or strings.get("FileVersion") != __version__ or strings.get("ProductVersion") != __version__
        or identity is None or identity.get("version") != ".".join(map(str, release_parts))):
    raise SystemExit("Package, Windows file/product version and manifest versions differ")
license_root = root / "licenses" / "stage7"
license_entries = json.loads((license_root / "manifest.json").read_text(encoding="utf-8"))
if {item["package"] for item in license_entries} != {"fonttools", "Brotli", "zopfli", "PyYAML", "tomli-w", "defusedxml"}:
    raise SystemExit("Missing stage-7 license manifest entries")
for item in license_entries:
    if version(item["package"]) != item["version"] or hashlib.sha256((license_root / item["file"]).read_bytes()).hexdigest() != item["sha256"]:
        raise SystemExit("Stage-7 licenses differ from build inputs. Review licenses and run tools/dependencies/collect_special_licenses.py")
vendor = root / "vendor" / "ffmpeg"
required = [vendor / name for name in ("ffmpeg.exe", "ffprobe.exe", "LICENSE", "README.txt")]
required += [root / "vendor" / "rhwp" / name for name in ("rhwp.exe", "LICENSE", "THIRD_PARTY_LICENSES.md",
             "msvcp140.dll", "vcruntime140.dll", "vcruntime140_1.dll", "runtime-licenses/LICENSE.RTF")]
missing = [str(path) for path in required if not path.is_file()]
if missing:
    raise SystemExit("Missing FFmpeg build assets. See vendor/ffmpeg/README-build.md:\n" + "\n".join(missing))

analysis = Analysis(
    [str(root / "run_magic_converter.py")],
    pathex=[str(root)],
    binaries=[(str(vendor / "ffmpeg.exe"), "vendor/ffmpeg"),
              (str(vendor / "ffprobe.exe"), "vendor/ffmpeg")] + collect_dynamic_libs("pypdfium2_raw") + collect_dynamic_libs("pymupdf"),
    datas=[(str(vendor / "LICENSE"), "vendor/ffmpeg"),
           (str(root / "assets" / "magic_converter-tool.ico"), "assets"),
           (str(root / "magic_converter" / "data" / "hwpx"), "magic_converter/data/hwpx"),
           (str(root / "magic_converter" / "data" / "hwp"), "magic_converter/data/hwp"),
           (str(root / "vendor" / "rhwp"), "vendor/rhwp"),
           (str(vendor / "README.txt"), "vendor/ffmpeg"),
           (str(root / "docs/legal/THIRD_PARTY_NOTICES.md"), "."),
           (str(root / "licenses"), "licenses"),
           (str(root / "docs/user/FORMATS.md"), "."),
           (str(root / "docs/reference/FORMAT_CONTRACTS.md"), "."),
           (str(root / "docs/user/USER_GUIDE.md"), "."),
           (str(root / "README.md"), ".")]
           + [(str(root / "packaging" / name), "build-metadata") for name in ("vendor-lock.json", "python-wheels.json", "notices-lock.json", "source-assets.json", "release-policy.json", "build-environment.json")]
           + collect_data_files("pypdfium2") + collect_data_files("pypdfium2_raw") + collect_data_files("pymupdf"),
    hiddenimports=["PIL.PngImagePlugin", "PIL.JpegImagePlugin", "PIL.WebPImagePlugin",
                   "PIL.BmpImagePlugin", "PIL.TiffImagePlugin", "PIL.AvifImagePlugin", "pillow_heif.HeifImagePlugin",
                   "markdown.extensions.tables", "markdown.extensions.fenced_code", "encodings.cp949",
                   "pythoncom", "pywintypes", "win32com.client", "win32com.client.dynamic",
                   "win32com.client.gencache", "win32com.shell.shell", "win32com.shell.shellcon",
                   "win32timezone", "win32job", "olefile", "fontTools.ttLib.woff2", "brotli", "zopfli.zlib",
                   "magic_converter.core.specialized.data_converter", "magic_converter.core.specialized.font_converter",
                   "magic_converter.core.specialized.ebook_converter", "magic_converter.core.specialized.email_converter",
                   "magic_converter.core.specialized.mesh_converter", "magic_converter.core.specialized.geo_converter"] + collect_submodules("fontTools.ttLib.tables"),
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=["tkinter", "PyQt5", "PyQt6", "PySide2", "pytest", "imageio_ffmpeg"],
    noarchive=False,
    optimize=1,
)
# Installer-generated RECORD/INSTALLER/REQUESTED/direct_url data is neither
# upstream wheel content nor runtime metadata. RECORD can contain entry-point
# hashes specific to an absolute venv path. Keep upstream METADATA/WHEEL/notices.
analysis.datas = [entry for entry in analysis.datas
                  if not (".dist-info/" in entry[0].replace("\\", "/")
                      and Path(entry[0]).name in {"RECORD", "INSTALLER", "REQUESTED", "direct_url.json"})]
inventory_files = create_inventory(analysis, root, Path(CONF["workpath"]) / "inventory")
analysis.datas += [("build-metadata/" + Path(source).name, source, "DATA") for source, _ in inventory_files]
pyz = PYZ(analysis.pure)
def build_executable(name, console=False):
    resource = versioninfo.load_version_info_from_text_file(root / "packaging" / "version_info.txt")
    for group in resource.kids:
        for table in getattr(group, "kids", []):
            for item in getattr(table, "kids", []):
                if getattr(item, "name", None) == "OriginalFilename":
                    item.val = name + ".exe"
                elif getattr(item, "name", None) == "InternalName":
                    item.val = name
    return EXE(
        pyz, analysis.scripts, analysis.binaries, analysis.datas, [],
        name=name,
        debug=False,
        bootloader_ignore_signals=False,
        strip=False,
        upx=False,
        console=console,
        disable_windowed_traceback=False,
        icon=str(root / "assets" / "magic_converter-tool.ico"),
        manifest=str(root / "packaging" / "app.manifest"),
        version=resource,
        uac_admin=False,
        uac_uiaccess=False,
    )

exe = build_executable("magic_converter")
cli_exe = build_executable("magic_converter-cli", console=True)
