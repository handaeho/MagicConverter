"""Capture the Python catalog and expected route plans from a pinned checkout."""

import argparse
from collections import Counter
from dataclasses import asdict
import json
from pathlib import Path
import subprocess
import sys


PROFILES = {
    "all": (True, True, True),
    "no_ffmpeg": (False, True, True),
    "no_presentation": (True, False, True),
    "no_rhwp": (True, True, False),
    "without_optional_engines": (False, False, False),
}
ENGINE_ORDER = ("ffmpeg", "presentations", "rhwp")


def dump(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def export(checkout, catalog_path, matrix_path):
    sys.path.insert(0, str(checkout))
    from magic_converter import __version__
    from magic_converter.cli import COMMANDS
    from magic_converter.core.conversion.builtin_converters import create_catalog
    from magic_converter.core.conversion.file_router import ALL_ACTIONS
    from magic_converter.core.conversion.formats import FORMATS
    from magic_converter.core.settings.preferences import default_preferences

    baseline = subprocess.check_output(
        ["git", "-C", str(checkout), "rev-parse", "HEAD"], text=True).strip()
    catalog = create_catalog(ffmpeg_available=True, presentation_available=True, rhwp_available=True)
    formats = [asdict(item) for item in FORMATS.known]
    actions = [asdict(action) for action in ALL_ACTIONS.values()]
    converters = [{"id": engine.id, "display_name": engine.display_name,
                   "max_parallelism": engine.max_parallelism,
                   "edges": [asdict(edge) for edge in engine.edges]}
                  for engine in catalog.converters]
    policies = {}
    for action in ALL_ACTIONS:
        policy = asdict(catalog.policy(action))
        if policy["sources"] is not None:
            policy["sources"] = sorted(policy["sources"])
        policies[action] = policy
    names = [f"input.{item.extensions[0]}" for item in FORMATS.known]
    names += ["input.unregistered", "no-extension"]
    # Alias detection, compound extensions and a mixed-case filename.
    names += sorted({f"Folder/한글.{ext.upper()}" for item in FORMATS.known for ext in item.extensions[1:]})
    counts = {}
    unavailable_reasons = {}
    engines = Counter()
    matrix_path.parent.mkdir(parents=True, exist_ok=True)
    with matrix_path.open("w", encoding="utf-8", newline="\n") as matrix:
        for profile, available in PROFILES.items():
            selected = create_catalog(ffmpeg_available=available[0], presentation_available=available[1],
                                      rhwp_available=available[2])
            disabled = [name for name, enabled in zip(ENGINE_ORDER, available, strict=True) if not enabled]
            for converter in selected.converters:
                if converter.id in disabled:
                    unavailable_reasons[converter.id] = converter.status.reason
            count = Counter()
            for name in names:
                for key in ALL_ACTIONS:
                    assessment = selected.assess(name, key)
                    row = {"profile": profile, "disabled": disabled, "path": name, "action": key,
                           "reason": assessment.reason, "route": asdict(assessment.route)
                           if assessment.route is not None else None}
                    count["queries"] += 1
                    if assessment.route is not None:
                        count["planned"] += 1
                        if assessment.route.steps:
                            count["file_routes"] += 1
                            if profile == "all" and name in names[:len(formats)]:
                                engines.update({edge.engine_id for edge in assessment.route.steps})
                    matrix.write(dump(row) + "\n")
            counts[profile] = dict(count)

    # Order is part of route tie-breaking and the public actions/formats output.
    data = {"baseline": baseline, "version": __version__, "commands": sorted(COMMANDS),
            "formats": formats, "actions": actions, "policies": policies,
            "converters": converters, "unavailable_reasons": unavailable_reasons,
            "default_preferences": default_preferences().to_dict()}
    catalog_path.parent.mkdir(parents=True, exist_ok=True)
    with catalog_path.open("w", encoding="utf-8", newline="\n") as file:
        # Keep each edge and format readable in diffs without expanding every field.
        file.write("{\n")
        for index, (key, value) in enumerate(data.items()):
            if index:
                file.write(",\n")
            file.write(f"  {dump(key)}: ")
            if key == "converters":
                file.write("[\n")
                for i, converter in enumerate(converters):
                    header = {k: v for k, v in converter.items() if k != "edges"}
                    file.write((" ,\n" if i else "") + "    " + dump(header)[:-1] + ',"edges":[\n')
                    file.write(",\n".join("      " + dump(edge) for edge in converter["edges"]))
                    file.write("\n    ]}")
                file.write("\n  ]")
            elif key in {"formats", "actions"}:
                file.write("[\n" + ",\n".join("    " + dump(item) for item in value) + "\n  ]")
            elif key == "policies":
                file.write("{\n" + ",\n".join(f"    {dump(action)}:{dump(policy)}"
                                                   for action, policy in policies.items()) + "\n  }")
            else:
                file.write(dump(value))
        file.write("\n}\n")
    summary = {"baseline": baseline, "version": __version__, "commands": len(COMMANDS), "formats": len(formats),
               "actions": len(actions), "engines": len(converters), "edges": sum(len(e["edges"]) for e in converters),
               "queries_by_profile": counts, "file_routes_by_engine": dict(engines)}
    matrix_path.with_suffix(".summary.json").write_text(dump(summary) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=True))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--catalog", type=Path, required=True)
    parser.add_argument("--matrix", type=Path, required=True)
    args = parser.parse_args()
    export(args.baseline.resolve(strict=True), args.catalog, args.matrix)


if __name__ == "__main__":
    main()
