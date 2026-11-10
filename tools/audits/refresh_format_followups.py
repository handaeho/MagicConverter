"""Recheck a pinned comparison CSV against current routes; never adds support."""

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter import __version__
from magic_converter.core.conversion.formats import FORMATS
from magic_converter.core.conversion.builtin_converters import create_catalog
from magic_converter.core.conversion.file_router import ALL_ACTIONS


def main():
    baseline = ROOT / "docs/reviews/history/FILEE_FORMAT_COMPARISON.csv"
    output = ROOT / "docs/reviews/FORMAT_FOLLOWUP.csv"
    rows = list(csv.DictReader(baseline.open(encoding="utf-8-sig", newline="")))
    local = list(FORMATS.known)
    ext_map = {ext: item for item in local for ext in item.extensions}
    catalog = create_catalog()
    routes = {f.id: {key: value.route for key in ALL_ACTIONS
                     if (value := catalog.assess(Path("sample." + f.extensions[0]), key)).route is not None}
              for f in local}
    changed = []
    for row in rows:
        matched = {ext_map[e].id for e in row["filee_extensions"].split(";") if e in ext_map}
        formats = [f for f in local if f.id in matched]
        current = {"magic_ids": ";".join(sorted(matched)),
                   "magic_declared_readable": str(any(f.readable for f in formats)),
                   "magic_declared_writable": str(any(f.writable for f in formats)),
                   "magic_convert_action_count": str(sum(sum(key not in {"copy_path", "terminal", "zip"} and not key.startswith("tool:") for key in routes[f.id]) for f in formats)),
                   "missing_aliases": ";".join(e for e in row["filee_extensions"].split(";") if e not in ext_map)}
        differences = [key for key, value in current.items() if row[key] != value]
        if differences:
            changed.append({"filee_id": row["filee_id"], "fields": differences})
        row.update(current)
        row["review_version"] = __version__
        row["registration_changed"] = str(bool(differences))
        row["followup_stage"] = "10" if row["category"] in {"Image", "Audio", "Video", "Archive", "Subtitle", "Document"} else "13"
        row["assessment"] = "preservation_and_direction_review" if matched else "optional_scope_requires_engine_and_fixtures"
        row["engine_ids"] = ";".join(sorted({step.engine_id for f in formats for route in routes[f.id].values() for step in route.steps}))
    with output.open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    summary = {"version": __version__, "baseline_filee_commit": "799e39485f2aa78677f9ba51f692e862a7af76a6",
               "note": "Pinned registry comparison only; routes/engine IDs do not prove conversion fidelity or installed capability.",
               "filee_ids": len(rows), "magic_ids": len(local), "magic_extensions": len(ext_map),
               "matched_filee_ids": sum(bool(r["magic_ids"]) for r in rows),
               "optional_unregistered_filee_ids": sum(not r["magic_ids"] for r in rows), "changes": changed}
    (ROOT / "docs/reviews/FORMAT_FOLLOWUP.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
