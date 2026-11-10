"""Run an external EPUBCheck on small outputs from the actual app pipeline."""

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--jar", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--allow-errors",
        action="store_true",
        help="Record invalid audit fixtures without a failing exit code",
    )
    args = parser.parse_args()
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=True)
    from PIL import Image
    from magic_converter.core.specialized.ebook_converter import Book, Chapter, write_epub
    from magic_converter.core.specialized.safe_html import image_uri
    from magic_converter.core.conversion.builtin_converters import create_catalog
    from magic_converter.core.conversion.conversion_request import prepare_request
    from magic_converter.core.conversion.conversion_execution import execute_file

    def run(source, key):
        request = prepare_request([source], key, create_catalog())
        return execute_file(
            request, request.files[0], None, lambda _: None, lambda _: None
        )

    source = folder / "source.epub"
    write_epub(
        Book(
            "Validation",
            ["Test Author"],
            "ko",
            chapters=[Chapter("One", "<p>Normal body <strong>text</strong>.</p>")],
        ),
        source,
    )
    fb2 = run(source, "ebook:fb2")
    fb2_epub = run(fb2, "document:epub")
    bmp = io.BytesIO()
    Image.new("RGB", (12, 8), "blue").save(bmp, format="BMP")
    bmp_epub = folder / "bitmap.epub"
    write_epub(
        Book(
            "Bitmap",
            language="en",
            chapters=[
                Chapter("One", '<p><img src="' + image_uri(bmp.getvalue()) + '"/></p>')
            ],
        ),
        bmp_epub,
    )
    markdown = folder / "source.md"
    markdown.write_text(
        "# Heading\n\nA paragraph.\n\n| A | B |\n|---|---|\n| 1 | 2 |\n",
        encoding="utf-8",
    )
    md_epub = run(markdown, "document:epub")
    records = []
    for name, path in (
        ("simple_epub", source),
        ("fb2_roundtrip", fb2_epub),
        ("bmp_image", bmp_epub),
        ("markdown_table", md_epub),
    ):
        report = folder / (name + "-epubcheck.json")
        report.unlink(missing_ok=True)
        result = subprocess.run(
            ["java", "-jar", str(args.jar.resolve()), str(path), "--json", str(report)],
            capture_output=True,
            timeout=40,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        stdout = result.stdout.decode("utf-8", errors="replace")
        stderr = result.stderr.decode("utf-8", errors="replace")
        details = (
            json.loads(report.read_text(encoding="utf-8")) if report.exists() else {}
        )
        record = {
            "case": name,
            "file": str(path),
            "exit_code": result.returncode,
            "stdout": stdout,
            "stderr": stderr,
            "checker": details.get("checker"),
            "messages": details.get("messages", []),
        }
        records.append(record)
        print(
            name, result.returncode, json.dumps(record["messages"], ensure_ascii=False)
        )
    (folder / "summary.json").write_text(
        json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return (
        0
        if args.allow_errors
        or all(r["exit_code"] == 0 and r["checker"] for r in records)
        else 1
    )


if __name__ == "__main__":
    raise SystemExit(main())
