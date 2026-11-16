"""Compare PDF object operations and bounded web input against the pinned Python app."""

import argparse
import base64
from collections import Counter
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import io
import json
import re
from pathlib import Path
import sys
from threading import Thread
from time import perf_counter

import pymupdf
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run
from tools.validation.verify_rust_image_parity import pixel_metrics


def fixture(path):
    with pymupdf.open() as document:
        for i in range(1, 4):
            page = document.new_page(width=100 + i * 15, height=125 + i * 5)
            page.insert_text((20, 40), f"Page {i}", fontsize=14)
            page.draw_rect((15, 55, 75, 105), color=(i * 0.2, 0.2, 0.3), fill=(0.7, 0.4, 0.2))
            page.add_text_annot((78, 70), f"Note {i}")
            if i == 3:
                page.set_rotation(90)
        document.set_metadata({"title": "Preserve this title", "author": "Author"})
        document.save(path)


def pdf_model(path):
    values, pixels = [], []
    with pymupdf.open(path) as document:
        for page in document:
            values.append({"rotation": page.rotation, "bounds": list(page.rect), "text": page.get_text(),
                           "images": len(page.get_images()), "drawings": len(page.get_drawings()),
                           "annotations": [annotation.info["content"] for annotation in page.annots()]})
            pixels.append(page.get_pixmap(matrix=pymupdf.Matrix(2, 2)).samples)
        properties = {k: document.metadata[k] for k in ("title", "author", "subject", "keywords")}
    return values, pixels, properties


def compare_pdf(left, right, *, image_pdf=False):
    a, a_pixels, a_properties = pdf_model(left)
    b, b_pixels, b_properties = pdf_model(right)
    dimensions = len(a) == len(b) and all(len(i) == len(j) for i, j in zip(a_pixels, b_pixels))
    metrics = pixel_metrics(a_pixels, b_pixels) if dimensions else None
    comparable_properties = a_properties.copy()
    if image_pdf and re.fullmatch(r"\.magic_converter-[a-z0-9_]+", a_properties["title"]):
        comparable_properties["title"] = ""
    return {"equal": a == b and comparable_properties == b_properties and metrics is not None and metrics["max"] == 0,
            "python": a, "rust": b, "python_properties": a_properties, "rust_properties": b_properties,
            "pixels": metrics, "python_bytes": left.stat().st_size, "rust_bytes": right.stat().st_size}


@contextmanager
def web_server(png):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            if self.path == "/redirect.png":
                self.send_response(302)
                self.send_header("Location", "/photo.png")
                self.end_headers()
                return
            body, headers = {
                "/photo.png": (png, {"Content-Type": "image/png",
                                    "Content-Disposition": "attachment; filename*=UTF-8''%ED%95%9C%EA%B8%80.png"}),
                "/html.png": (b"<html><body>Log in</body></html>", {"Content-Type": "text/html"}),
                "/blocked.png": (b"MZ" + png, {}),
                "/oversize.png": (b"", {"Content-Length": str(512 * 1024 * 1024 + 1)}),
                "/truncated.png": (png[:12], {"Content-Length": str(len(png))}),
                "/wrong.png": (b"%PDF-1.7\n", {}),
            }.get(self.path, (b"", {}))
            self.send_response(200 if self.path != "/missing.png" else 404)
            if "Content-Length" not in headers:
                self.send_header("Content-Length", str(len(body)))
            for key, value in headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

    with ThreadingHTTPServer(("127.0.0.1", 0), Handler) as server:
        thread = Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield f"http://127.0.0.1:{server.server_port}"
        finally:
            server.shutdown()
            thread.join()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    evidence = {"worker_sha256": digest(args.worker), "pdf": {}, "web": {}, "status": "incomplete"}
    source = root / "fixture.pdf"
    fixture(source)
    picture = Image.new("RGBA", (72, 60), (240, 61, 99, 145))
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG")
    png = buffer.getvalue()
    image_paths = []
    for kind in ("png", "bmp", "jpeg", "tiff", "webp"):
        name = root / f"image.{kind}"
        image = picture.convert("RGB") if kind in ("jpeg", "bmp") else picture
        image.save(name)
        image_paths.append(name)

    def convert(label, action, paths):
        records = []
        for backend, prefix, environment in (
            ("python", [sys.executable, "-B", args.baseline.resolve() / "run_magic_converter.py"], None),
            ("rust", [args.worker.resolve()], native_environment()),
        ):
            folder = root / label / backend
            folder.mkdir(parents=True)
            start = perf_counter()
            code, report = run(prefix, ("convert", *paths, "--action", action, "--output", folder,
                                        "--data-dir", root / f"profile-{backend}", "--json"), environment=environment)
            records.append((code, report, perf_counter() - start))
        return records

    try:
        cases = [(key, [source]) for key in ("tool:pdf_select", "tool:pdf_split", "tool:pdf_rotate", "tool:pdf_compress", "strip")]
        cases.extend([("tool:pdf_merge", [source, image_paths[0]]), ("document:pdf", image_paths)])
        for action, inputs in cases:
            left, right = convert(action.replace(":", "-"), action, inputs)
            records = {"codes": [left[0], right[0]], "python_s": left[2], "rust_s": right[2], "outputs": []}
            evidence["pdf"][action] = records
            if left[0] or right[0]:
                records["reports"] = [left[1], right[1]]
                continue
            a = [Path(path) for path in left[1]["jobs"][0]["outputs"]]
            b = [Path(path) for path in right[1]["jobs"][0]["outputs"]]
            for p, r in zip(a, b, strict=True):
                if p.is_dir():
                    for path in sorted(p.glob("*.pdf")):
                        records["outputs"].append(compare_pdf(path, r / path.name))
                else:
                    records["outputs"].append(compare_pdf(p, r, image_pdf=action == "document:pdf"))
        with web_server(png) as address:
            web_cases = {
                "attachment": ([address + "/photo.png"], 0),
                "redirect": ([address + "/redirect.png"], 0),
                "data-url": (["data:image/png;base64," + base64.b64encode(png).decode()], 0),
                "html": ([address + "/html.png"], 1),
                "executable": ([address + "/blocked.png"], 1),
                "oversize": ([address + "/oversize.png"], 1),
                "truncated": ([address + "/truncated.png"], 1),
                "wrong-type": ([address + "/wrong.png"], 1),
                "missing": ([address + "/missing.png"], 1),
                "mixed": ([address + "/photo.png", image_paths[0]], 1),
                "credentials": ([address.replace("://", "://name:pw@") + "/photo.png"], 1),
            }
            for label, (paths, expected) in web_cases.items():
                left, right = convert(label, "image:png", paths)
                item = {"codes": [left[0], right[0]], "expected": expected,
                        "python_s": left[2], "rust_s": right[2]}
                evidence["web"][label] = item
                if left[0] == right[0] == 0:
                    a, b = (Path(record[1]["jobs"][0]["outputs"][0]) for record in (left, right))
                    item.update(equal=a.name == b.name and digest(a) == digest(b), outputs=[a.name, b.name])
                else:
                    item["reports"] = [left[1], right[1]]
                    item["no_published_files"] = all(not list((root / label / mode).iterdir()) for mode in ("python", "rust"))
        evidence["pdf_counts"] = dict(Counter("equal" if output["equal"] else "different"
                                      for case in evidence["pdf"].values() for output in case["outputs"]))
        evidence["status"] = "complete"
    finally:
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
