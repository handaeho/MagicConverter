"""Frozen local-library routes, folder results and timeout cleanup."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
import pymupdf
from docx import Document
from PIL import Image
from pptx import Presentation
from magic_converter import __version__
from magic_converter.core.jobs.process_runner import start_process, stop_process
from tools.validation.verify_resource_limits import processes


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    exe = args.executable.resolve()
    env = os.environ.copy()
    env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    env["PYTHONUTF8"] = "1"
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "MAGIC_CONVERTER_FFMPEG", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(key, None)
    pdf = folder / "two.pdf"
    with pymupdf.open() as doc:
        for text in ("First page", "Second page"):
            doc.new_page().insert_text((20, 40), text)
        doc.save(pdf)
    image = folder / "image.png"
    Image.new("RGB", (32, 16), "red").save(image)
    source = folder / "document.docx"
    document = Document(); document.add_paragraph("Frozen library worker"); document.save(source)
    before = {p: hashlib.sha256(p.read_bytes()).hexdigest() for p in (pdf, image, source)}

    def command(path, action, label, *extra):
        return [str(exe), "convert", str(path), "--action", action, "--output", str(folder / label),
                "--data-dir", str(folder / "profile"), "--json", *extra]

    def cli(path, action, label, *, success=True):
        result = subprocess.run(command(path, action, label), env=env, capture_output=True, timeout=120)
        response = json.loads(result.stdout.decode("utf-8-sig"))
        assert result.returncode == (0 if success else 1), response
        assert response["ok"] == success, response
        return Path(response["jobs"][0]["outputs"][0]) if success else response

    with Image.open(cli(image, "image:jpg", "image-output")) as result:
        assert result.size == (32, 16)
    pages = cli(pdf, "document:png", "page-output")
    assert len(list(pages.glob("*.png"))) == 2
    with pymupdf.open(cli(pdf, "tool:pdf_rotate", "rotate-output")) as result:
        assert [p.rotation for p in result] == [90, 90]
    assert "Frozen library worker" in cli(source, "document:html", "document-output").read_text(encoding="utf-8")
    text = folder / "notes.txt"
    text.write_text("Headless Qt text rendering", encoding="utf-8")
    assert len(list(cli(text, "document:png", "text-pages").glob("*.png"))) == 1
    deck = Presentation(cli(pdf, "pptx_editable", "editable-output"))
    assert len(deck.slides) == 2
    assert any("First page" in s.text for s in deck.slides[0].shapes if s.has_text_frame)
    invalid = folder / "invalid.pdf"
    invalid.write_bytes(b"not a PDF")
    cli(invalid, "document:png", "invalid-output", success=False)
    assert not list((folder / "invalid-output").glob("*"))

    # The frozen deadline includes worker startup. Source regressions separately
    # wait until each real library stub is entered before requesting interruption.
    previous = os.environ.copy()
    os.environ.clear(); os.environ.update(env)
    try:
        process, job = start_process(command(pdf, "document:png", "timeout-output", "--timeout", ".3"),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        os.environ.clear(); os.environ.update(previous)
    started, owned = time.monotonic(), {}
    try:
        while process.poll() is None:
            tree = processes()
            discovered, previous_ids = {process.pid}, set()
            while discovered != previous_ids:
                previous_ids = discovered.copy()
                discovered.update(pid for pid, (parent, _) in tree.items() if parent in discovered)
            owned.update({pid: tree[pid][1] for pid in discovered if pid in tree})
            if time.monotonic() - started > 45:
                raise RuntimeError("Frozen library timeout exceeded integration deadline")
            time.sleep(.05)
        stdout, stderr = process.communicate(timeout=5)
        response = json.loads(stdout.decode("utf-8-sig"))
        assert process.returncode == 1 and response["jobs"][0]["timed_out"], (response, stderr)
        assert not list((folder / "timeout-output").glob("*"))
        remaining = processes()
        lingering = [pid for pid, stamp in owned.items() if pid in remaining and remaining[pid][1] == stamp]
        assert not lingering, lingering
    finally:
        stop_process(process, job)
    elapsed = round(time.monotonic() - started, 3)
    assert cli(image, "image:jpg", "recovery-output").is_file()
    assert all(hashlib.sha256(p.read_bytes()).hexdigest() == sha for p, sha in before.items())
    result = {"status": "passed", "version": __version__, "exe_sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
              "normal": ["image", "document", "editable_pdf", "pdf_rotation", "pdf_folder_pages", "headless_qt_text_pdf"],
              "invalid_input": "refused_without_publication", "input_hashes_preserved": True,
              "timeout": {"requested_seconds": .3, "total_process_seconds": elapsed,
                          "owned_pids_observed": sorted(owned), "lingering_owned_pids": lingering, "published": False},
              "next_conversion": "passed", "note": "Total time includes one-file startup and process observation."}
    (folder / "report.json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
