"""B05 model payload and frozen B06 timeout/owned-process integration evidence."""

import argparse
import io
import json
import os
import random
import subprocess
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from bs4 import BeautifulSoup
from PIL import Image
from magic_converter.core.specialized.ebook_converter import Book, Chapter, read_book, write_epub
from magic_converter.core.specialized.safe_html import image_uri, decode_image
from tools.audits.audit_followups import epubcheck
from magic_converter.core.jobs.process_runner import start_process, stop_process


def processes():
    import win32com.client
    return {int(p.ProcessId): (int(p.ParentProcessId), str(p.CreationDate))
            for p in win32com.client.GetObject("winmgmts:").ExecQuery("SELECT ProcessId,ParentProcessId,CreationDate FROM Win32_Process")}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--jar", type=Path, required=True)
    args = parser.parse_args()
    folder = args.output.resolve()
    folder.mkdir(parents=True, exist_ok=False)
    stream = io.BytesIO()
    Image.frombytes("RGB", (128, 128), random.Random(19).randbytes(128 * 128 * 3)).save(stream, "PNG")
    image = stream.getvalue()
    uri = image_uri(image)
    source = folder / "repeated.epub"
    write_epub(Book("Repeated", language="en", cover=uri, chapters=[
        Chapter("One", '<p>' + f'<img src="{uri}"/>' * 32 + '</p>'),
        Chapter("Two", f'<p><img src="{uri}"/></p>')]), source)
    book = read_book(source, "epub")
    body_bytes = sum(len(ch.body.encode("utf-8")) for ch in book.chapters)
    assert body_bytes < len(image) and len(book.assets) == 1 and book.assets.resolve(book.cover)[0] == image
    checks = [epubcheck(source, folder, args.jar.resolve(), "java")]
    env = os.environ.copy()
    env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    env["PYTHONUTF8"] = "1"
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "MAGIC_CONVERTER_FFMPEG", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(key, None)

    def command(path, target, label, *extra):
        return [str(args.executable.resolve()), "convert", str(path), "--to", target, "--output", str(folder / label),
                "--data-dir", str(folder / "profile"), "--json", *extra]

    def cli(path, target, label):
        process = subprocess.run(command(path, target, label), env=env, capture_output=True, timeout=120)
        response = json.loads(process.stdout.decode("utf-8-sig"))
        assert process.returncode == 0, response
        return Path(response["jobs"][0]["outputs"][0])

    output = cli(source, "epub", "epub-output")
    checks.append(epubcheck(output, folder, args.jar.resolve(), "java"))
    assert all(c["exit"] == 0 and not c["messages"] for c in checks)
    with zipfile.ZipFile(output) as archive:
        images = [archive.read(n) for n in archive.namelist() if "/images/" in n]
        assert images == [image]
    exported = cli(source, "html", "html-output").read_text(encoding="utf-8")
    assert "mc-asset:" not in exported
    images = BeautifulSoup(exported, "html.parser").find_all("img")
    assert len(images) == 33 and all(decode_image(n["src"])[0] == image for n in images)
    large = folder / "large.csv"
    with large.open("w", encoding="utf-8", newline="") as stream:
        stream.write("name,value\n")
        for _ in range(100000):
            stream.write("row,value1234567890\n")
    started = time.monotonic()
    # Apply the same restricted PATH for this command while retaining an outer
    # ownership guard for unexpected verifier failures. Observe lingering PIDs
    # before closing this guard, so it cannot hide application cleanup failures.
    previous_env = os.environ.copy()
    os.environ.update(env)
    for key in set(previous_env) - set(env):
        os.environ.pop(key, None)
    try:
        process, job = start_process(command(large, "json", "timeout-output", "--timeout", ".3"),
                                     stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    finally:
        os.environ.clear()
        os.environ.update(previous_env)
    try:
        owned = {}
        while process.poll() is None:
            tree = processes()
            discovered = {process.pid}
            previous = set()
            while discovered != previous:
                previous = discovered.copy()
                discovered.update(pid for pid, (parent, _) in tree.items() if parent in discovered)
            owned.update({pid: tree[pid][1] for pid in discovered if pid in tree})
            if time.monotonic() - started > 30:
                stop_process(process, job)
                job = None
                raise RuntimeError("Frozen timeout integration exceeded its test deadline")
            time.sleep(.05)
        stdout, stderr = process.communicate(timeout=5)
        response = json.loads(stdout.decode("utf-8-sig"))
        assert process.returncode == 1 and response["jobs"][0]["timed_out"], (response, stderr.decode(errors="replace"))
        assert not list((folder / "timeout-output").glob("*"))
        elapsed = round(time.monotonic() - started, 3)
        remaining = processes()
        lingering = [pid for pid, created in owned.items() if pid in remaining and remaining[pid][1] == created]
        assert not lingering, lingering
    finally:
        stop_process(process, job)
    normal = folder / "normal.json"
    normal.write_text('{"x":42}', encoding="utf-8")
    assert "42" in cli(normal, "yaml", "recovery-output").read_text(encoding="utf-8")
    result = {"status": "passed", "model": {"image_bytes": len(image), "references": 33,
              "body_bytes": body_bytes, "unique_assets": len(book.assets), "unique_asset_bytes": book.assets.byte_size,
              "total_payload_bytes": body_bytes + book.assets.byte_size, "package_bytes": source.stat().st_size},
              "epubcheck": checks, "html_images_preserved": len(images),
              "frozen_timeout": {"requested_seconds": .3, "total_process_seconds": elapsed,
              "owned_pids_observed": sorted(owned), "lingering_owned_pids": lingering, "published": False},
              "next_conversion": "passed", "note": "Payload bytes are not process RSS. Library-stall and memory-limit cases are covered by real source worker tests."}
    (folder / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
