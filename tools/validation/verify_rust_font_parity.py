"""Check the twelve font container routes against the pinned Python signature."""

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from time import perf_counter

from fontTools.fontBuilder import FontBuilder
from fontTools.pens.t2CharStringPen import T2CharStringPen
from fontTools.pens.ttGlyphPen import TTGlyphPen
from fontTools.ttLib import TTFont

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run


def draw(pen):
    pen.moveTo((50, 0))
    pen.lineTo((250, 500))
    pen.lineTo((450, 0))
    pen.closePath()


def font(path, *, cff=False):
    builder = FontBuilder(1000, isTTF=not cff)
    glyphs = [".notdef", "A", "uniD55C"]
    builder.setupGlyphOrder(glyphs)
    builder.setupCharacterMap({65: "A", 0xD55C: "uniD55C"})
    if cff:
        outlines = {}
        for glyph in glyphs:
            pen = T2CharStringPen(550, None)
            if glyph != ".notdef":
                draw(pen)
            outlines[glyph] = pen.getCharString()
        builder.setupCFF("MagicFixture", {"FullName": "Magic Fixture", "FamilyName": "Magic Fixture", "Weight": "Regular"}, outlines, {})
    else:
        outlines = {}
        for glyph in glyphs:
            pen = TTGlyphPen(None)
            if glyph != ".notdef":
                draw(pen)
            outlines[glyph] = pen.glyph()
        builder.setupGlyf(outlines)
    builder.setupHorizontalMetrics({glyph: (550, 0) for glyph in glyphs})
    builder.setupHorizontalHeader(ascent=800, descent=-200)
    builder.setupNameTable({"familyName": "Magic Fixture", "styleName": "Regular", "uniqueFontIdentifier": "Magic Fixture", "fullName": "Magic Fixture", "psName": "MagicFixture", "licenseDescription": "Synthetic test font"})
    builder.setupOS2(sTypoAscender=800, sTypoDescender=-200, usWinAscent=800, usWinDescent=200, fsType=4)
    builder.setupPost()
    builder.save(path)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    baseline = args.baseline.resolve()
    sys.path.insert(0, str(baseline))
    from magic_converter.core.specialized.font_converter import signature

    ttf = inputs / "font.ttf"
    font(ttf)
    cff = inputs / "cff.otf"
    font(cff, cff=True)
    for fmt in ("otf", "woff", "woff2"):
        with TTFont(ttf, recalcTimestamp=False) as source:
            source.flavor = fmt if fmt.startswith("woff") else None
            source.save(inputs / f"font.{fmt}")
    routes = [(inputs / f"font.{src}", dst, 0) for src in ("ttf", "otf", "woff", "woff2")
              for dst in ("ttf", "otf", "woff", "woff2") if src != dst]
    routes += [(cff, "ttf", 1), (cff, "woff", 0), (cff, "woff2", 0)]
    evidence = {"worker_sha256": digest(args.worker), "cases": {}}
    try:
        for src, dst, expected in routes:
            label = f"{src.name}-{dst}"
            values = []
            checks = {}
            for name, prefix, environment in (
                ("python", [sys.executable, "-B", baseline / "run_magic_converter.py"], None),
                ("rust", [args.worker.resolve()], native_environment()),
            ):
                folder = root / label / name
                folder.mkdir(parents=True)
                start = perf_counter()
                code, report = run(prefix, ("convert", src, "--action", f"font:{dst}", "--output", folder,
                                            "--data-dir", root / f"profile-{name}", "--json"), environment=environment)
                checks[name] = {"code": code, "s": perf_counter() - start, "report": report}
                files = list(folder.iterdir())
                if code == 0:
                    with TTFont(files[0], recalcTimestamp=False) as result:
                        values.append(signature(result))
                    checks[name]["bytes"] = files[0].stat().st_size
                else:
                    values.append(files)
            checks["equal"] = all(checks[name]["code"] == expected for name in ("python", "rust")) and values[0] == values[1]
            evidence["cases"][label] = checks
    finally:
        evidence["counts"] = Counter("equal" if case["equal"] else "different" for case in evidence["cases"].values())
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if evidence["counts"].get("different"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
