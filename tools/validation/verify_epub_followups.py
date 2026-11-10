"""EPUBCheck and actual source/frozen conversions for A01/A02, with refusals."""

import argparse
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path
from urllib.parse import quote, unquote, urldefrag
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.audits.audit_followups import conversion, epubcheck
from magic_converter.core.specialized.ebook_converter import Book, Chapter, XHTML, write_epub


def fixture(folder):
    path = folder / "valid.epub"
    tags = ("div", "section", "aside", "article", "main", "figure")
    ids = ("plain", "p%20q", "p%2520q", "한글%23", "literal%", "a#b")
    chapters = []
    for index in range(2):
        body = ""
        for identity in (*ids, *("container-" + tag for tag in tags)):
            fragment = quote(identity, safe="")
            body += (f'<div id="{identity}"><p>kept <a href="#{fragment}">self</a>'
                     f'<a href="chapter{1-index}.xhtml#{fragment}">other</a></p></div>')
        chapters.append(Chapter("Chapter " + str(index), body))
    write_epub(Book("Followup controls", language="en", chapters=chapters), path)
    with zipfile.ZipFile(path) as archive:
        contents = [(i, archive.read(i)) for i in archive.infolist()]
    with zipfile.ZipFile(path, "w") as archive:
        for info, data in contents:
            if info.filename.startswith("EPUB/chapter"):
                document = ET.fromstring(data)
                for node in document.iter():
                    if node.get("id", "").startswith("container-"):
                        node.tag = f"{{{XHTML}}}" + node.get("id").removeprefix("container-")
                data = ET.tostring(document, encoding="utf-8", xml_declaration=True)
            archive.writestr(info, data)
    return path


def links(path):
    with zipfile.ZipFile(path) as archive:
        docs = {name: ET.fromstring(archive.read(name)) for name in archive.namelist() if name.startswith("EPUB/chapter")}
    count = cross = 0
    for name, doc in docs.items():
        for anchor in doc.iter(f"{{{XHTML}}}a"):
            file, fragment = urldefrag(anchor.get("href", ""))
            target = "EPUB/" + file if file else name
            if target not in docs or unquote(fragment) not in {n.get("id") for n in docs[target].iter()}:
                raise ValueError("Missing/changed EPUB link: " + anchor.get("href", ""))
            count += 1
            cross += target != name
    if (count, cross) != (48, 24):
        raise ValueError("Lost or redirected links: " + str((count, cross)))
    return {"links": count, "cross_chapter": cross}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--java", default="java")
    parser.add_argument("--executable", type=Path)
    args = parser.parse_args()
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    records = []

    def convert(path):
        if not args.executable:
            return conversion(path)
        env = os.environ.copy()
        env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
        for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "MAGIC_CONVERTER_FFMPEG"):
            env.pop(key, None)
        result = subprocess.run([str(args.executable.resolve()), "convert", str(path), "--to", "epub",
                                 "--output", str(folder / (path.stem + "-converted")),
                                 "--data-dir", str(folder / "profile"), "--json"], env=env, capture_output=True, timeout=90)
        response = json.loads(result.stdout.decode("utf-8-sig"))
        job = response["jobs"][0]
        return {"status": "failed" if result.returncode else "passed", "error": job.get("errors"),
                "output": job.get("outputs", [None])[0] if job.get("outputs") else None}

    path = fixture(folder)
    for generation in range(4):
        checked = epubcheck(path, folder, args.jar.resolve(), args.java)
        record = {"generation": generation, "file": str(path), "epubcheck": checked, **links(path)}
        records.append(record)
        if checked["exit"] or any(m.get("severity") in ("ERROR", "FATAL", "WARNING") for m in checked["messages"]):
            raise ValueError("Valid EPUB rejected by EPUBCheck")
        if generation != 3:
            converted = convert(path)
            if converted["status"] != "passed":
                raise ValueError("Valid EPUB refused: " + str(converted))
            path = Path(converted["output"])
    for name, body in (("missing", '<section id="p%20q"><p>kept</p></section><a href="#p%20q">wrong decode</a>'),
                       ("active", '<script id="unsafe">discard</script><a href="#unsafe">missing</a>')):
        source = folder / (name + ".md")
        source.write_text(body, encoding="utf-8")
        result = convert(source)
        published = list(folder.rglob(name + "*.epub"))
        if result["status"] != "failed" or published:
            raise ValueError("Invalid input published: " + name)
        records.append({"case": name, "rejected": True, "published": published, "error": result.get("error")})
    report = {"status": "passed", "mode": "frozen" if args.executable else "source", "cases": records}
    (folder / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"status": "passed", "report": str(folder / "report.json")}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
