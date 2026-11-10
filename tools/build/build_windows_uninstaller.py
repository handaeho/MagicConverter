"""Build the standalone removal tool for the Windows 2.2.29 installer."""

import argparse
import gzip
import os
from pathlib import Path, PurePosixPath
import tempfile

from build_public_windows import ROOT, compile_exe, files, sha, zip_bytes


INVENTORY = "packaging/public-windows/uninstall-files.txt"
ROOT_FILES = {"magic_converter.exe", "magic_converter-cli.exe", "magic_converter-runtime.txt"}
SOURCE = """
LICENSE assets/README.md assets/magic_converter-tool.ico assets/magic_converter-tool.svg
docs/user/UNINSTALL_WINDOWS.md packaging/app.manifest packaging/public-windows/uninstall.cs
packaging/public-windows/process_arguments.cs packaging/public-windows/install_location.cs
tools/build/build_windows_uninstaller.py
tools/build/build_public_windows.py
""".split()


def normalize_inventory(path):
    entries = path.read_text(encoding="utf-8").splitlines()
    if not ROOT_FILES.issubset(entries) or len({name.casefold() for name in entries}) != len(entries):
        raise ValueError("Missing or duplicate removal inventory entries")
    for name in entries:
        parts = PurePosixPath(name).parts
        if (not parts or "\\" in name or ":" in name or ".." in parts or PurePosixPath(name).as_posix() != name
                or name not in ROOT_FILES and (len(parts) < 2 or parts[0] not in {"app", "runtime"})):
            raise ValueError("Invalid removal inventory path: " + name)
    return ("\n".join(sorted(entries)) + "\n").encode("utf-8")


def build(output, inventory):
    contents = normalize_inventory(inventory)
    source_zip = zip_bytes({**files(SOURCE), INVENTORY: contents})
    output = output.resolve()
    if output.exists():
        raise FileExistsError("Choose an unused output directory: " + str(output))
    output.parent.mkdir(parents=True, exist_ok=True)
    compiler = Path(os.environ["SystemRoot"]) / "Microsoft.NET/Framework64/v4.0.30319/csc.exe"
    with tempfile.TemporaryDirectory(prefix="magic-uninstall-", dir=output.parent) as work:
        staging = Path(work)
        ready = staging / "release"
        ready.mkdir()
        (staging / "version.cs").write_text(
            "using System.Reflection;\n"
            '[assembly: AssemblyTitle("MagicConverter Uninstaller")]\n'
            '[assembly: AssemblyVersion("2.2.29.0")]\n'
            '[assembly: AssemblyFileVersion("2.2.29.0")]\n',
            encoding="ascii",
        )
        (staging / "uninstall-files.gz").write_bytes(gzip.compress(contents, mtime=0))
        compile_exe(compiler, staging, ready / "Uninstall.exe", "uninstall.cs", "winexe",
                    "/reference:System.IO.Compression.dll", "/reference:Microsoft.CSharp.dll",
                    "/resource:uninstall-files.gz,uninstall-files.gz",
                    str(ROOT / "packaging/public-windows/install_location.cs"))
        (ready / "Uninstall-corresponding-source.zip").write_bytes(source_zip)
        (ready / "SHA256SUMS.txt").write_text("".join(
            f"{sha(path.read_bytes())}  {path.name}\n" for path in sorted(ready.iterdir())
        ), encoding="ascii")
        ready.rename(output)
    print(output)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--inventory", type=Path, default=ROOT / INVENTORY)
    options = parser.parse_args()
    build(options.output, options.inventory)
