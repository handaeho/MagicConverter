"""Check that editable PDF-to-PowerPoint keeps diagram labels and open tables."""

import argparse
import json
from pathlib import Path
import subprocess
from xml.etree import ElementTree as ET
from zipfile import ZipFile

import pymupdf


def label(page, text, center, baseline):
    width = pymupdf.get_text_length(text, fontname="helv", fontsize=10)
    page.insert_text((center - width / 2, baseline), text, fontname="helv", fontsize=10)


def source_pdf(path):
    blue = (0.31, 0.50, 0.74)
    with pymupdf.open() as doc:
        diagram = doc.new_page(width=360, height=240)
        diagram.draw_rect((40, 45, 170, 205), color=blue, width=0.7)
        diagram.draw_rect((40, 20, 170, 45), color=None, fill=(0.3, 0.5, 0.7))
        label(diagram, "Workflow", 105, 37)
        for i, text in enumerate(("Intake", "Review", "Publish")):
            y = 55 + 49 * i
            diagram.draw_rect((47, y, 163, y + 34), color=None, fill=(0.86, 0.86, 0.86))
            label(diagram, text, 105, y + 20)

        grid = doc.new_page(width=360, height=240)
        xs, ys = [30, 115, 225, 325], [55, 75, 95, 115, 135]
        for x in xs[1:]:
            grid.draw_line((x, ys[0]), (x, ys[-1]), color=blue, width=0.25)
        for y in ys[1:]:
            grid.draw_line((xs[0], y), (xs[-1], y), color=blue, width=0.25)
        for r, names in enumerate((
            ("Header A", "Header B", "Header C"),
            ("September", "November", "December"),
            ("Ottawa", "Toronto", "Montreal"),
            ("Review", "Draft", "Final"),
        )):
            for c, text in enumerate(names):
                label(grid, text, (xs[c] + xs[c + 1]) / 2, ys[r] + 14)
        doc.save(path)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    source = output / "layout.pdf"
    source_pdf(source)
    run = subprocess.run(
        [args.candidate.resolve(), "convert", "--action", "pptx_editable",
         "--data-dir", output / "profile", "--output", output, source],
        check=True, capture_output=True, timeout=30,
    )
    assert json.loads(run.stdout)["ok"]
    with ZipFile(output / "layout_converted.pptx") as result:
        panel, grid = [
            ET.fromstring(result.read(f"ppt/slides/slide{i}.xml")) for i in (1, 2)
        ]
    assert panel.find(".//{*}tbl") is None
    labels = {}
    for shape in panel.findall(".//{*}sp"):
        text = "".join(t.text or "" for t in shape.findall(".//{*}t"))
        if text:
            labels[text] = int(shape.find(".//{*}off").get("y"))
    assert labels["Intake"] + 500_000 < labels["Review"]
    assert labels["Review"] + 500_000 < labels["Publish"]
    table = grid.find(".//{*}tbl")
    assert table is not None
    rows = table.findall("{*}tr")
    assert len(rows) == 4 and all(len(row.findall("{*}tc")) == 3 for row in rows)
    first = rows[0].find("{*}tc")
    assert first.find(".//{*}t").text == "Header A"
    assert first.find(".//{*}pPr").get("algn") == "ctr"
    assert first.find("{*}tcPr").get("anchor") == "ctr"
    assert first.find("{*}tcPr/{*}lnL/{*}noFill") is not None
    assert first.find("{*}tcPr/{*}lnT/{*}noFill") is not None
    print("Diagram labels remain editable; all 12 cells and open borders are preserved.")


if __name__ == "__main__":
    main()
