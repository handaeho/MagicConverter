"""Compare ebook chapters, metadata, links, images and editable Word structure."""

import argparse
import base64
from hashlib import sha256
from io import BytesIO
import json
from pathlib import Path
import sys
from zipfile import ZipFile

from bs4 import BeautifulSoup
from docx import Document
from PIL import Image
from urllib.parse import unquote, urldefrag

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from magic_converter.core.specialized.ebook_converter import Book, Chapter, read_book, write_epub, write_fb2
from tools.validation.verify_rust_full_parity import digest, native_environment, run
from tools.validation.verify_rust_office_render_parity import compare_pdf
from tests.regression.test_epub_protection import fixture as protected_fixture


def image_data(color, size):
    file = BytesIO()
    Image.new("RGB", size, color).save(file, "PNG")
    return file.getvalue()


def fixtures(root):
    root.mkdir()
    p1 = image_data("red", (80, 95))
    p2 = image_data("blue", (90, 95))
    book = Book("한글 도서", ["Author"], "ko", "Description", "Publisher")
    asset = book.assets.add(p1)
    book.cover = asset
    book.chapters = [
        Chapter("One", '<h1 id="main">One</h1><p>한글 <strong>bold</strong> and <em>italic</em>'
                       '<a href="#한글%2520">note</a></p><table><tr><td rowspan="2">merged</td><td>A</td></tr>'
                       f'<tr><td>B <img src="{asset}" alt="image"/></td></tr></table>'),
        Chapter("Two", '<h1 id="한글%20">Two</h1><p>Chapter two. <a href="#main">back</a></p>'),
    ]
    write_epub(book, root / "book.epub")
    write_fb2(book, root / "book.fb2", lambda _: None)
    (root / "plain.txt").write_text("한국어 text\nline two\n", encoding="utf-8")
    (root / "markup.md").write_text("# 한국어 Heading\n\nAn **emphasis**\n\n| A | B |\n|---|---|\n| x | y |\n", encoding="utf-8")
    doc = Document()
    doc.add_paragraph("한국어 Word")
    doc.add_paragraph().add_run("bold").bold = True
    doc.add_picture(BytesIO(p1))
    doc.save(root / "source.docx")
    with ZipFile(root / "comic.cbz", "w") as package:
        package.writestr("folder/page10.png", p2)
        package.writestr("folder/page2.png", p1)
    for filename, settings in (("obfuscated", {}), ("drm", {"marker": "META-INF/rights.xml"}),
                               ("bad-font", {"media": "application/xhtml+xml"})):
        folder = root / filename
        folder.mkdir()
        protected_fixture(folder, **settings).rename(root / f"{filename}.epub")
    (root / "bad-image.md").write_text('<p><img src="data:image/jpeg;base64,' +
        base64.b64encode(p1).decode() + '"></p>', encoding="utf-8")


def image_model(data):
    with Image.open(BytesIO(data)) as image:
        rgb = image.convert("RGBA")
        return rgb.size, sha256(rgb.tobytes()).hexdigest()


def html_model(html, book=None):
    soup = BeautifulSoup(html, "html.parser")
    tags = []
    for tag in soup.find_all():
        name = tag.name
        if name in {"p", "li", "strong", "em", "h1", "h2", "h3", "td", "th", "pre", "code", "a"}:
            attrs = {key: tag.get(key) for key in ("rowspan", "colspan") if tag.get(key)}
            tags.append((name, tag.get_text(" ", strip=True), attrs))
        if name == "img" and book is not None:
            tags.append((name, image_model(book.assets.resolve(tag["src"])[0])))
    return tags


def ebook_model(path):
    kind = path.suffix[1:]
    book = read_book(path, kind)
    cover = image_model(book.assets.resolve(book.cover)[0]) if book.cover else None
    chapters = [(name, html_model(html, book)) for name, html in ((item.title,item.body) for item in book.chapters)]
    return book.title, book.authors, book.language, book.description, book.publisher, cover, chapters


def docx_model(path):
    doc = Document(path)
    props = doc.core_properties
    paras = [(p.text, tuple((r.text, bool(r.bold), bool(r.italic)) for r in p.runs if r.text))
             for p in doc.paragraphs]
    tables = [[[c.text for c in row.cells] for row in table.rows] for table in doc.tables]
    with ZipFile(path) as package:
        images = sorted(image_model(package.read(name)) for name in package.namelist() if name.startswith("word/media/"))
    return props.title, props.author, props.language, props.comments, paras, tables, images


def links(path):
    with ZipFile(path) as package:
        documents = {name: BeautifulSoup(package.read(name), "xml") for name in package.namelist() if name.startswith("EPUB/chapter")}
    links = 0
    for name, document in documents.items():
        for anchor in document.find_all("a", href=True):
            if ":" in anchor["href"]:
                continue
            file, fragment = urldefrag(anchor["href"])
            target = f"EPUB/{file}" if file else name
            assert target in documents
            if fragment:
                assert documents[target].find(id=unquote(fragment)) is not None
            links += 1
    return links


def compare(left, right, action):
    if action == "document:pdf":
        return compare_pdf(left,right)
    if action in {"document:epub", "ebook:fb2"}:
        a,b = ebook_model(left),ebook_model(right)
        anchor = (links(left),links(right)) if action == "document:epub" else None
        result = {"equal": a == b and (anchor is None or anchor[0] == anchor[1]), "links": anchor}
    elif action == "document:docx":
        a,b = docx_model(left),docx_model(right)
        result = {"equal": a == b}
    else:
        a,b = (path.read_text(encoding="utf-8") for path in (left,right))
        if action == "document:html":
            a,b = html_model(a),html_model(b)
        if action == "document:md":
            a,b = [([line for line in value.splitlines() if line.startswith("# ")], html_model(value)) for value in (a,b)]
        result = {"equal": a == b}
    if not result["equal"]:
        result.update(python_model=a,rust_model=b)
    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    inputs = root / "inputs"
    fixtures(inputs)
    actions = [(f"book.{kind}", action, 0) for kind in ("epub","fb2") for action in
               ("document:epub","ebook:fb2","document:docx","document:txt","document:md","document:html")]
    actions += [(name,action,0) for name in ("plain.txt","markup.md","source.docx") for action in ("document:epub","ebook:fb2")]
    actions += [("comic.cbz",action,0) for action in ("document:epub","document:pdf")]
    actions += [(name,"document:epub",code) for name,code in (
        ("obfuscated.epub",0),("drm.epub",1),("bad-font.epub",1),("bad-image.md",1))]
    report = {"worker_sha256": digest(args.worker), "cases": {}}
    try:
        for filename,action,expected in actions:
            key = f"{filename}-{action.replace(':','-')}"
            print(key,flush=True)
            record = {"equal": False}
            report["cases"][key] = record
            outputs = []
            for edition,command,env in (("python",[sys.executable,"-B",args.baseline.resolve()/"run_magic_converter.py"],None),
                                        ("rust",[args.worker.resolve()],native_environment())):
                folder = root/key/edition
                folder.mkdir(parents=True)
                code,receipt = run(command,("convert","--action",action,"--output",folder,"--data-dir",folder.parent/f"{edition}-profile",inputs/filename),environment=env)
                record[edition] = {"code":code,"receipt":receipt}
                outputs.append(list(folder.iterdir()))
            if all(record[edition]["code"] == expected for edition in ("python","rust")):
                if expected:
                    record["equal"] = all(not output for output in outputs)
                else:
                    record.update(compare(outputs[0][0],outputs[1][0],action))
    finally:
        report["equal"] = sum(row["equal"] for row in report["cases"].values())
        report["total"] = len(report["cases"])
        (root/"report.json").write_text(json.dumps(report,ensure_ascii=False,indent=2),encoding="utf-8")
    if report["equal"] != len(actions):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
