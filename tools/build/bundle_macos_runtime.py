"""Copy and relocate every non-system Mach-O dependency into an app bundle."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess


def run(*arguments):
    return subprocess.check_output(arguments, text=True).strip()


def rpaths(binary):
    lines = run("otool", "-l", str(binary)).splitlines()
    result = []
    for index, line in enumerate(lines):
        if line.strip() == "cmd LC_RPATH":
            result.append(lines[index + 2].strip().split(" (offset", 1)[0].removeprefix("path "))
    return result


def bundle(app, entries, search):
    frameworks = app / "Contents/Frameworks"
    frameworks.mkdir(parents=True, exist_ok=True)
    queue = [(path.resolve(), path.resolve()) for path in entries]
    copied = {}
    processed = set()
    inventory = []
    while queue:
        original, target = queue.pop(0)
        if target in processed:
            continue
        processed.add(target)
        paths = [Path(value.replace("@loader_path", str(original.parent))) for value in rpaths(original)]
        paths += search
        dependencies = run("otool", "-L", str(original)).splitlines()[1:]
        for row in dependencies:
            dependency = row.strip().split(" (compatibility", 1)[0]
            if dependency.startswith(("/usr/lib/", "/System/Library/")):
                continue
            if dependency.startswith("@rpath/"):
                suffix = dependency.removeprefix("@rpath/")
                candidates = [root / suffix for root in paths]
            elif dependency.startswith("@loader_path/"):
                candidates = [original.parent / dependency.removeprefix("@loader_path/")]
            elif dependency.startswith("@executable_path/"):
                candidates = [original.parent / dependency.removeprefix("@executable_path/")]
            else:
                candidates = [Path(dependency)]
                if not Path(dependency).is_absolute():
                    candidates += [root / Path(dependency).name for root in search]
            source = next((path.resolve() for path in candidates if path.is_file()), None)
            if source is None:
                raise RuntimeError(f"Unresolved dependency {dependency} of {original}")
            if source == original:
                subprocess.run(["install_name_tool", "-id", f"@rpath/{target.name}", target], check=True)
                continue
            destination = frameworks / source.name
            previous = copied.get(destination)
            if previous is not None and previous != source:
                if source.read_bytes() != previous.read_bytes():
                    raise RuntimeError(f"Conflicting library names: {source} and {previous}")
            if previous is None:
                if destination != source:
                    shutil.copy2(source, destination)
                    destination.chmod(0o755)
                copied[destination] = source
                queue.append((source, destination))
            relative = os.path.relpath(destination, target.parent)
            subprocess.run(["install_name_tool", "-change", dependency, f"@loader_path/{relative}", target], check=True)
        for value in rpaths(target):
            if value.startswith(("/opt/homebrew/", "/usr/local/", "/Users/runner/")):
                subprocess.run(["install_name_tool", "-delete_rpath", value, target], check=True)
        inventory.append({"path": target.relative_to(app).as_posix(),
                          "source": str(original), "sha256": hashlib.sha256(original.read_bytes()).hexdigest()})
    for target in sorted(processed):
        subprocess.run(["codesign", "--force", "--sign", "-", target], check=True)
    notices = app / "Contents/Resources/Notices/Native"
    notices.mkdir(parents=True, exist_ok=True)
    cellars = set()
    for source in copied.values():
        if "Cellar" in source.parts:
            index = source.parts.index("Cellar")
            cellars.add(Path(*source.parts[:index + 3]))
    for cellar in cellars:
        destination = notices / cellar.parent.name
        destination.mkdir(exist_ok=True)
        for pattern in ("LICENSE*", "COPYING*", "NOTICE*", "AUTHORS*", ".brew/*.rb"):
            for source in cellar.glob(pattern):
                if source.is_file():
                    shutil.copy2(source, destination / source.name)
    (notices / "runtime-inventory.json").write_text(json.dumps(inventory, indent=2) + "\n")
    print(f"Bundled {len(copied)} native libraries and relocated {len(processed)} Mach-O files.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--app", type=Path, required=True)
    parser.add_argument("--search", type=Path, action="append", default=[])
    parser.add_argument("entries", type=Path, nargs="+")
    arguments = parser.parse_args()
    bundle(arguments.app.resolve(), arguments.entries, arguments.search)
