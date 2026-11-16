"""Compare native Office PDF exports, chained page rendering and PDF containers."""

import argparse
import hashlib
import json
from pathlib import Path
import sys
from zipfile import ZipFile

from docx import Document
from openpyxl import Workbook
from PIL import Image
from pptx import Presentation
import pymupdf

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run
from tools.validation.verify_rust_image_parity import image_model, pixel_metrics


def fixtures(root):
    root.mkdir()
    doc = Document()
    doc.sections[0].header.paragraphs[0].text = "Header"
    doc.add_heading("문서 Title", 1)
    doc.add_paragraph("Editable text")
    table = doc.add_table(rows=2, cols=2)
    table.cell(0, 0).text = "First"
    table.cell(1, 1).text = "표"
    doc.sections[0].footer.paragraphs[0].text = "Footer"
    doc.save(root / "word.docx")
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "한국어"
    sheet.append(["Label", "Units"])
    sheet.append(["Test", 15])
    sheet.append(["문서", 22])
    sheet.page_setup.orientation = "landscape"
    workbook.save(root / "automatic.xlsx")
    sheet.print_area = "A1:B3"
    sheet.print_title_rows = "1:1"
    workbook.save(root / "print.xlsx")
    sheet.print_area = "A1:Z5000"
    workbook.save(root / "refuse.xlsx")
    workbook.close()
    ppt = Presentation()
    slide = ppt.slides.add_slide(ppt.slide_layouts[1])
    slide.shapes.title.text = "슬라이드"
    slide.placeholders[1].text = "Slide with text"
    ppt.save(root / "slides.pptx")
    with pymupdf.open() as pdf:
        for width, height in [(640, 480), (320, 640)]:
            page = pdf.new_page(width=width, height=height)
            page.insert_text((72, 60), "Selectable PDF text")
            page.draw_rect(pymupdf.Rect(30, 100, 150, 280), fill=(.7, .2, .1))
        pdf.save(root / "pages.pdf")


def pdf_contents(path):
    with pymupdf.open(path) as document:
        return [((page.rect.width, page.rect.height), page.get_text(), page.get_pixmap().samples)
                for page in document]


def compare_pdf(left, right):
    before, after = pdf_contents(left), pdf_contents(right)
    if [x[0] for x in before] != [x[0] for x in after]:
        return {"equal": False, "python_geometry": [x[0] for x in before], "rust_geometry": [x[0] for x in after]}
    pixels = pixel_metrics([x[2] for x in before], [x[2] for x in after])
    text_equal = [x[1] for x in before] == [x[1] for x in after]
    return {"equal": text_equal and pixels["max"] == 0, "text_equal": text_equal, "pixels": pixels, "pages": len(before)}


def compare_folders(left, right):
    files = sorted(left.iterdir())
    a = {file.name: image_model(file) for file in files}
    b = {file.name: image_model(file) for file in sorted(right.iterdir())}
    return {"equal": a == b, "pages": len(a)}


def compare_zip(left, right):
    def members(path):
        with ZipFile(path) as z:
            return [(name, hashlib.sha256(z.read(name)).hexdigest()) for name in z.namelist()]
    return {"equal": members(left) == members(right), "pages": len(members(left))}


def compare_decks(left, right):
    def model(path):
        deck = Presentation(path)
        slides = [[(shape.left, shape.top, shape.width, shape.height, hashlib.sha256(shape.image.blob).hexdigest())
                   for shape in slide.shapes] for slide in deck.slides]
        return (deck.slide_width, deck.slide_height), slides
    a, b = model(left), model(right)
    return {"equal": a == b, "python": a, "rust": b}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--pdf-only", action="store_true")
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    inputs = root / "inputs"
    fixtures(inputs)
    cases = [
        ("pages.pdf", "ebook:cbz", compare_zip),
        ("pages.pdf", "pptx_image", compare_decks),
    ]
    if not args.pdf_only:
        cases += [
            ("word.docx", "document:pdf", compare_pdf),
            ("word.docx", "document:png", compare_folders),
            ("slides.pptx", "document:pdf", compare_pdf),
            ("slides.pptx", "document:jpg", compare_folders),
            ("print.xlsx", "office:xlsx_print_pdf", compare_pdf),
            ("automatic.xlsx", "office:xlsx_print_pdf", compare_pdf),
            ("refuse.xlsx", "office:xlsx_print_pdf", None),
        ]
    record = {"worker_sha256": digest(args.worker), "cases": {}}
    try:
        for filename, action, compare in cases:
            key = f"{filename}-{action.replace(':', '-')}"
            print(key, flush=True)
            outcomes, results = [], []
            for backend, cmd, env in (
                ("python", [sys.executable, "-B", args.baseline.resolve() / "run_magic_converter.py"], None),
                ("rust", [args.worker.resolve()], native_environment()),
            ):
                folder = root / key / backend
                folder.mkdir(parents=True)
                status, report = run(cmd, ("convert", "--timeout", 60, "--action", action, "--data-dir", root / backend,
                                           "--output", folder, inputs / filename), environment=env)
                outcomes.append({"status": status, "report": report})
                results.append(list(folder.iterdir()))
            expected = 0 if compare else 1
            outcome_match = [result["status"] for result in outcomes] == [expected, expected]
            record["cases"][key] = {"python": outcomes[0], "rust": outcomes[1], "equal": False}
            case = record["cases"][key]
            if compare and outcome_match and all(len(items) == 1 for items in results):
                case.update(compare(results[0][0], results[1][0]))
            elif not compare and outcome_match:
                case["equal"] = all(not items for items in results)
    finally:
        record["equal"] = sum(case["equal"] for case in record["cases"].values())
        record["total"] = len(record["cases"])
        (root / "report.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    if record["equal"] != len(cases):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
