"""Reproducible source/registry census, not a claim of semantic parity.

Reads an already checked-out Filee revision. It never executes upstream code,
installs engines, changes app settings or accesses user conversion inputs.
"""

import argparse
import ast
import csv
import hashlib
import json
import re
import subprocess
import sys
from collections import Counter
from dataclasses import fields
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def git(reference, *args):
    return subprocess.check_output(
        ["git", "-C", str(reference), *args], text=True, encoding="utf-8"
    ).strip()


def read_registry(reference):
    source = (reference / "src/Filee.Core/Formats/FormatRegistry.cs").read_text(
        encoding="utf-8-sig"
    )
    pattern = r'new\(\s*"([^"\n]+)"\s*,\s*"([^"\n]+)"\s*,\s*FormatCategory\.(\w+)\s*,\s*\[([^\]]*)\]([^)]*)\)'
    result = []
    for identity, label, category, extensions, flags in re.findall(
        pattern, source, re.S
    ):
        result.append(
            {
                "id": identity,
                "label": label,
                "category": category,
                "extensions": re.findall(r'"([^"]+)"', extensions),
                "readable": "Readable: false" not in flags,
                "writable": "Writable: false" not in flags,
            }
        )
    if not result or len({f["id"] for f in result}) != len(result):
        raise ValueError("Upstream registry syntax changed; review extraction")
    return result


def inventory(reference):
    result = []
    for path in sorted((reference / "src").rglob("*")):
        if not path.is_file() or path.suffix not in {
            ".cs",
            ".csproj",
            ".json",
            ".axaml",
        }:
            continue
        data = path.read_bytes()
        result.append(
            {
                "path": path.relative_to(reference).as_posix(),
                "bytes": len(data),
                "lines": len(data.splitlines()),
                "sha256": hashlib.sha256(data).hexdigest(),
            }
        )
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("reference", type=Path)
    parser.add_argument("--previous", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reference, output = args.reference.resolve(), args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    from magic_converter.core.conversion.formats import FORMATS
    from magic_converter.core.conversion.builtin_converters import create_catalog
    from magic_converter.core.conversion.file_router import ALL_ACTIONS
    from magic_converter.core.settings import preferences

    catalog = create_catalog()
    upstream = read_registry(reference)
    local = list(FORMATS.known)
    ext_map = {ext: item for item in local for ext in item.extensions}
    action_routes = {}
    for item in local:
        source = Path("sample." + item.extensions[0])
        action_routes[item.id] = {
            key: [
                f"{e.source}>{e.target}:{e.engine_id}" for e in availability.route.steps
            ]
            for key in ALL_ACTIONS
            if (availability := catalog.assess(source, key)).route is not None
        }
    rows = []
    for item in upstream:
        matched = {ext_map[e].id for e in item["extensions"] if e in ext_map}
        matches = [f for f in local if f.id in matched]
        rows.append(
            {
                "filee_id": item["id"],
                "category": item["category"],
                "filee_extensions": ";".join(item["extensions"]),
                "filee_readable": item["readable"],
                "filee_writable": item["writable"],
                "magic_ids": ";".join(sorted(matched)),
                "magic_declared_readable": any(f.readable for f in matches),
                "magic_declared_writable": any(f.writable for f in matches),
                "magic_convert_action_count": sum(
                    sum(
                        key not in {"copy_path", "terminal", "zip"}
                        and not key.startswith("tool:")
                        for key in action_routes[f.id]
                    )
                    for f in matches
                ),
                "missing_aliases": ";".join(
                    e for e in item["extensions"] if e not in ext_map
                ),
                "status": (
                    "shared_format_requires_fidelity_review"
                    if matches
                    else "not_registered"
                ),
            }
        )
    with (output / "format-comparison.csv").open(
        "w", encoding="utf-8-sig", newline=""
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    source_inventory = inventory(reference)
    engines = []
    for record in source_inventory:
        path = reference / record["path"]
        if not path.name.endswith("Converter.cs") or not record["path"].startswith(
            "src/Filee.Engines/"
        ):
            continue
        source = path.read_text(encoding="utf-8-sig")
        if not re.search(r"\bIConverter\b", source):
            continue
        identity = re.search(r'\bstring\s+Id\s*=>\s*"([^"]+)"', source)
        engines.append(
            {"path": record["path"], "id": identity.group(1) if identity else None}
        )
    option_groups = {}
    for file in (
        "src/Filee.Core/Presets/Preset.cs",
        "src/Filee.Core/Presets/OutputRule.cs",
        "src/Filee.Core/Settings/AppSettings.cs",
        "src/Filee.Core/Profiles/ToolbarProfile.cs",
    ):
        group = file
        for line in (reference / file).read_text(encoding="utf-8-sig").splitlines():
            match = re.search(r"\bclass\s+(\w+)", line)
            if match:
                group = match.group(1)
            match = re.search(r"\bpublic\s+[\w<>?\[\],]+\s+(\w+)\s*\{\s*get;", line)
            if match:
                option_groups.setdefault(group, []).append(match.group(1))
    local_options = {
        name: [f.name for f in fields(getattr(preferences, name))]
        for name in (
            "EncoderOptions",
            "ToolOptions",
            "OutputRule",
            "Preset",
            "WheelProfile",
            "Preferences",
        )
    }
    parsed = []
    for folder in (ROOT / "magic_converter", ROOT / "tests", ROOT / "tools"):
        for path in sorted(folder.rglob("*.py")):
            ast.parse(path.read_text(encoding="utf-8-sig"), filename=str(path))
            parsed.append(path.relative_to(ROOT).as_posix())
    totals = {}
    for category in sorted({f["category"] for f in upstream}):
        items = [r for r in rows if r["category"] == category]
        totals[category] = {
            "filee_ids": len(items),
            "shared_ids_by_extension": sum(bool(r["magic_ids"]) for r in items),
            "missing_ids": [r["filee_id"] for r in items if not r["magic_ids"]],
        }
    report = {
        "reference": {
            "head": git(reference, "rev-parse", "HEAD"),
            "commit": git(reference, "log", "-1", "--format=%cI %s"),
        },
        "local_head": git(ROOT, "rev-parse", "HEAD"),
        "scope_note": "Static registry/capability census. Shared extension is not fidelity, runtime availability or feature parity. Generic ZIP/copy/terminal actions do not prove conversion support.",
        "filee_format_ids": len(upstream),
        "filee_extensions": len({e for f in upstream for e in f["extensions"]}),
        "magic_format_ids": len(local),
        "magic_extensions": len(ext_map),
        "shared_filee_ids_by_extension": sum(bool(r["magic_ids"]) for r in rows),
        "missing_filee_ids": [r["filee_id"] for r in rows if not r["magic_ids"]],
        "registry_direction_candidates": [
            r["filee_id"]
            for r in rows
            if r["magic_ids"]
            and (
                r["filee_readable"]
                and not r["magic_declared_readable"]
                or r["filee_writable"]
                and not r["magic_declared_writable"]
            )
        ],
        "categories": totals,
        "upstream_source_inventory": source_inventory,
        "upstream_engine_entrypoints": engines,
        "upstream_option_groups": option_groups,
        "local_option_groups": local_options,
        "local_ast_parsed_files": parsed,
        "local_action_routes": action_routes,
    }
    if args.previous:
        old = read_registry(args.previous)
        report["previous_reference"] = {
            "head": git(args.previous, "rev-parse", "HEAD"),
            "format_ids": len(old),
            "changed_paths": git(
                reference,
                "diff",
                "--name-only",
                git(args.previous, "rev-parse", "HEAD"),
                "HEAD",
            ).splitlines(),
            "added_format_ids": sorted(
                {f["id"] for f in upstream} - {f["id"] for f in old}
            ),
        }
    (output / "inventory.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                key: report[key]
                for key in (
                    "reference",
                    "local_head",
                    "filee_format_ids",
                    "filee_extensions",
                    "magic_format_ids",
                    "magic_extensions",
                    "shared_filee_ids_by_extension",
                    "registry_direction_candidates",
                    "categories",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    print(
        "Engine entrypoints:",
        len(engines),
        "Indexed upstream source/config files:",
        len(source_inventory),
        "Parsed local Python files:",
        len(parsed),
    )


if __name__ == "__main__":
    main()
