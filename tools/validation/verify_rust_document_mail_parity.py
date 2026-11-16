"""Compare plain documents, MIME, PDFium and optional RAR data through both CLIs."""

import argparse
from collections import Counter
from email.message import EmailMessage
from email import policy
import hashlib
import io
import json
from pathlib import Path
import plistlib
import sys
import tarfile
from time import perf_counter
from zipfile import ZipFile

from bs4 import BeautifulSoup
from docx import Document
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run
from tools.validation.verify_rust_pdf_web_parity import fixture as pdf_fixture


def fixtures(folder):
    plain = folder / "문서.txt"
    plain.write_bytes("  first 한글\nsecond <value>\n\nthird\tvalue".encode("utf-16-le"))
    markdown = folder / "문서.md"
    markdown.write_text("# Heading 한글\n\nbody with **strong** text\n\n- One\n- Two\n\n```text\nexample\n```\n", encoding="utf-8")
    rtf_content = r"{\rtf1\ansi\ansicpg1252\uc1{\fonttbl{\f0 Arial;}} A \u-10916?\u-20992?\par second\tab row.}"
    rich = folder / "문서.rtf"
    rich.write_text(rtf_content, encoding="ascii")
    bundle = folder / "bundle.rtfd"
    with ZipFile(bundle, "w") as package:
        package.writestr("TXT.rtf", rtf_content)
    clipping = folder / "clip.textClipping"
    clipping.write_bytes(plistlib.dumps({"public.utf8-plain-text": "한글 & example\nsecond".encode()}, fmt=plistlib.FMT_BINARY))

    image = Image.new("RGBA", (4, 3), (40, 60, 128, 220))
    data = io.BytesIO()
    image.save(data, format="PNG")
    mail = EmailMessage(policy=policy.SMTP)
    mail["From"] = "Sender <sender@example.invalid>"
    mail["To"] = "Receiver <receiver@example.invalid>"
    mail["Subject"] = "한글 주제"
    mail.set_content("plain alternative")
    mail.add_alternative('<html><body><script>drop this</script><p>HTML 한글 &amp; body</p>'
                         '<img src="cid:test-image"><img src="https://example.invalid/never.png">'
                         '<a href="javascript:bad()" onclick="bad()">safe words</a></body></html>', subtype="html")
    mail.get_payload()[-1].add_related(data.getvalue(), maintype="image", subtype="png", cid="<test-image>")
    mail.add_attachment(bytes(range(64)), maintype="application", subtype="octet-stream", filename="첨부.bin")
    eml = folder / "메일.eml"
    eml.write_bytes(mail.as_bytes())
    mhtml = folder / "메일.mhtml"
    mhtml.write_bytes(mail.get_payload()[0].get_payload()[-1].as_bytes())
    pdf = folder / "문서.pdf"
    pdf_fixture(pdf)
    return (plain, markdown, rich, bundle, clipping), eml, mhtml, pdf


def document(path):
    if path.suffix == ".docx":
        return [(p.style.name.lower(), p.text) for p in Document(path).paragraphs]
    if path.suffix == ".epub":
        with ZipFile(path) as package:
            return {name: package.read(name).decode("utf-8") for name in package.namelist() if not name.endswith(".opf")}
    if path.suffix == ".html":
        soup = BeautifulSoup(path.read_text(encoding="utf-8"), "html.parser")
        return [soup.title.get_text() if soup.title else None,
                [(node.name, sorted(node.attrs.items()), node.get_text(" ", strip=True))
                 for node in soup.body.find_all() if node.name != "tbody"],
                soup.body.get_text(" ", strip=True)]
    return path.read_text(encoding="utf-8")


def archive(path):
    if path.suffix == ".zip":
        with ZipFile(path) as zip_file:
            return [(name, json.loads(zip_file.read(name)) if name == "manifest.json" else hashlib.sha256(zip_file.read(name)).hexdigest()) for name in zip_file.namelist()]
    with tarfile.open(path) as tar_file:
        return [(entry.name + "/" if entry.isdir() else entry.name,
                 hashlib.file_digest(tar_file.extractfile(entry), "sha256").hexdigest() if entry.isfile() else None)
                for entry in tar_file]


def raster(path):
    values = []
    for page in sorted(path.iterdir()):
        with Image.open(page) as image:
            values.append((page.name, image.size, tuple(round(v) for v in image.info["dpi"]),
                           hashlib.sha256(image.convert("RGB").tobytes()).hexdigest()))
    return values


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--rar", type=Path, action="append", default=[])
    args = parser.parse_args()
    root = args.output.resolve()
    folder = root / "inputs"
    folder.mkdir(parents=True)
    docs, eml, mhtml, pdf = fixtures(folder)
    routes = [(path, f"document:{target}", document) for path in docs
              for target in ("txt", "md", "html", "docx", "epub")
              if target != "epub" or path.suffix not in (".md", ".txt")]
    routes += [(path, f"document:{target}", document) for path in (eml, mhtml, pdf)
               for target in (("txt", "md", "html") if path == pdf else ("txt", "html"))]
    routes.append((eml, "email:attachments", archive))
    routes += [(pdf, f"document:{target}", raster) for target in ("png", "jpg")]
    routes += [(path.resolve(), f"archive:{target}", archive) for path in args.rar for target in ("zip", "tar", "gzip")]
    evidence = {"worker_sha256": digest(args.worker), "cases": {}}
    try:
        for src, action, model in routes:
            key = f"{src.name}-{action.replace(':', '-')}"
            checks = {}
            values = []
            for name, prefix, environment in (
                ("python", [sys.executable, "-B", args.baseline.resolve() / "run_magic_converter.py"], None),
                ("rust", [args.worker.resolve()], native_environment()),
            ):
                destination = root / key / name
                destination.mkdir(parents=True)
                start = perf_counter()
                code, report = run(prefix, ("convert", src, "--action", action, "--output", destination,
                                            "--data-dir", root / f"profile-{name}", "--json"), environment=environment)
                checks[name] = {"code": code, "s": perf_counter() - start, "report": report}
                values.append({path.name: model(path) for path in sorted(destination.iterdir())})
            checks["equal"] = checks["python"]["code"] == checks["rust"]["code"] == 0 and values[0] == values[1]
            if not checks["equal"]:
                checks["models"] = values
            evidence["cases"][key] = checks
    finally:
        evidence["counts"] = Counter("equal" if case["equal"] else "different" for case in evidence["cases"].values())
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if evidence["counts"].get("different"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
