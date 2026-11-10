"""Exercise the console EXE without Python/FFmpeg on its PATH."""
import argparse
import base64
import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from xml.etree import ElementTree as ET

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
parser = argparse.ArgumentParser()
parser.add_argument("executable")
parser.add_argument("--report", required=True)
parser.add_argument("--installed", action="store_true", help="Use the EXE beside its installed app and runtime")
args = parser.parse_args()
original = Path(args.executable).resolve()
root = ROOT / ".test-artifacts" / ("cli-standalone-" + str(time.time_ns()))
root.mkdir(parents=True)
executable = original if args.installed else root / "magic_converter-cli.exe"
if not args.installed:
    shutil.copy2(original, executable)
environment = os.environ.copy()
environment["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
for key in ("MAGIC_CONVERTER_FFMPEG", "PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
    environment.pop(key, None)
profile = root / "isolated profile"


def cli(*arguments, expect=0):
    result = subprocess.run([str(executable), *map(str, arguments), "--data-dir", str(profile), "--json"],
                            env=environment, cwd=root, capture_output=True, timeout=40,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    text = result.stdout.decode("utf-8-sig")
    if result.returncode != expect:
        raise RuntimeError(f"CLI exit {result.returncode}: {text}\n{result.stderr.decode('utf-8', errors='replace')}")
    return json.loads(text)


report = {"status": "running", "exe": str(original), "bytes": original.stat().st_size}
try:
    png = root / "한글 사진.png"
    Image.new("RGB", (64, 48), (22, 125, 210)).save(png)
    capabilities = cli("actions")
    if not capabilities.get("ok"):
        raise RuntimeError("CLI actions failed")
    converted = cli("convert", png, "--to", "jpg", "--output", root / "converted")
    with Image.open(converted["jobs"][0]["outputs"][0]) as image:
        assert image.size == (64, 48)
    inbox = root / "inbox"
    inbox.mkdir()
    shutil.copy2(png, inbox / png.name)
    first = cli("watch", inbox, "--action", "image:webp", "--settle", ".1", "--once")
    second = cli("watch", inbox, "--action", "image:webp", "--settle", ".1", "--once")
    assert len(first["jobs"]) == 1 and not second["jobs"]
    report.update(cli_conversion="passed", watch_restart_dedup="passed")
    structured = root / "중첩 자료.json"
    structured.write_text(json.dumps({"name": "한글", "nested": [True, None, 9007199254740993]}), encoding="utf-8")
    data_result = cli("convert", structured, "--to", "yaml", "--output", root / "data output")
    import yaml
    assert yaml.safe_load(Path(data_result["jobs"][0]["outputs"][0]).read_text(encoding="utf-8")) == json.loads(structured.read_text(encoding="utf-8"))
    from magic_converter.core.diagnostics.special_diagnostics import sample_font
    from fontTools.ttLib import TTFont
    font = root / "synthetic.ttf"
    sample_font(font)
    font_result = cli("convert", font, "--to", "woff2", "--output", root / "font output")
    with TTFont(font_result["jobs"][0]["outputs"][0]) as checked:
        assert checked.getBestCmap() == {65: "A", 0xAC00: "ga"} and checked["OS/2"].fsType == 2
    report.update(special_data="passed", special_woff2="passed")

    # Audit regressions must pass through the copied EXE's conversion engine.
    ordinary_xml = root / "ordinary.xml"
    ordinary_xml.write_text('<magic-data version="1"><customer id="42">Alice</customer></magic-data>', encoding="utf-8")
    xml_result = cli("convert", ordinary_xml, "--to", "json", "--output", root / "xml output")
    model = json.loads(Path(xml_result["jobs"][0]["outputs"][0]).read_text(encoding="utf-8"))
    assert model["$xml"]["children"][0]["text"] == "Alice"
    kml = root / "schema.kml"
    kml.write_text('<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Schema id="s"><SimpleField name="population" type="int"/></Schema><Placemark><ExtendedData><SchemaData schemaUrl="#s"><SimpleData name="population">42</SimpleData></SchemaData></ExtendedData><Point><coordinates>127,37</coordinates></Point></Placemark></Document></kml>', encoding="utf-8")
    kml_result = cli("convert", kml, "--to", "geojson", "--output", root / "kml output")
    feature = json.loads(Path(kml_result["jobs"][0]["outputs"][0]).read_text(encoding="utf-8"))["features"][0]
    assert feature["properties"]["population"] == 42
    bitmap = io.BytesIO()
    Image.new("RGB", (7, 5), (22, 87, 210)).save(bitmap, "BMP")
    fb2 = root / "heading-bitmap.fb2"
    fb2.write_text('<FictionBook xmlns="http://www.gribuser.ru/xml/fictionbook/2.0" xmlns:l="http://www.w3.org/1999/xlink"><description><title-info><book-title>Regression</book-title><lang>en</lang><coverpage><image l:href="#bmp"/></coverpage></title-info></description><body><section><title><p>One <emphasis>italic</emphasis></p><p>Two</p></title><p>Body<image l:href="#bmp"/></p></section></body><binary id="bmp" content-type="image/bmp">' + base64.b64encode(bitmap.getvalue()).decode("ascii") + '</binary></FictionBook>', encoding="utf-8")
    epub_result = cli("convert", fb2, "--to", "epub", "--output", root / "epub output")
    epub = Path(epub_result["jobs"][0]["outputs"][0])
    with zipfile.ZipFile(epub) as archive:
        chapter = ET.fromstring(archive.read("EPUB/chapter0.xhtml"))
        heading = chapter.find(".//{http://www.w3.org/1999/xhtml}h2")
        assert heading is not None and not heading.findall(".//{http://www.w3.org/1999/xhtml}p")
        assert "One italicTwo" == "".join(heading.itertext())
        opf = ET.fromstring(archive.read("EPUB/package.opf"))
        images = [n for n in opf.iter() if n.get("media-type", "").startswith("image/")]
        assert len(images) == 1 and images[0].get("media-type") == "image/png"
        with Image.open(io.BytesIO(archive.read("EPUB/" + images[0].get("href")))) as checked:
            assert checked.size == (7, 5) and checked.getpixel((0, 0)) == (22, 87, 210)
    report.update(audit_xml="passed", audit_kml_schema="passed", audit_fb2_heading="passed", audit_bmp_png="passed", audit_epub=str(epub))

    same_stat_inbox = root / "same-stat inbox"
    same_stat_inbox.mkdir()
    watched = same_stat_inbox / "data.json"
    watched.write_bytes(b'{"x":1}')
    original_stat = watched.stat()
    same_stat_output = root / "same-stat output"
    with (root / "same-stat-watch.log").open("wb") as log:
        watching = subprocess.Popen([str(executable), "watch", str(same_stat_inbox), "--to", "yaml",
                                    "--settle", ".1", "--duration", "14", "--output", str(same_stat_output),
                                    "--data-dir", str(profile), "--json"], cwd=root, env=environment,
                                    stdout=log, stderr=log, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            deadline = time.monotonic() + 30
            changed_at = None
            while time.monotonic() < deadline:
                outputs = list(same_stat_output.rglob("*.yaml"))
                if outputs and changed_at is None:
                    watched.write_bytes(b'{"x":2}')
                    os.utime(watched, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
                    changed_at = time.monotonic()
                if len(outputs) == 2:
                    elapsed = time.monotonic() - changed_at
                    assert sorted(yaml.safe_load(p.read_text(encoding="utf-8"))["x"] for p in outputs) == [1, 2]
                    assert watched.stat().st_size == original_stat.st_size and watched.stat().st_mtime_ns == original_stat.st_mtime_ns
                    report["watch_equal_stat"] = {"status": "passed", "seconds_after_change": round(elapsed, 2), "outputs": 2}
                    break
                if watching.poll() is not None:
                    raise RuntimeError("Persistent watch exited without detecting the same-stat change")
                time.sleep(.1)
            else:
                raise TimeoutError("Persistent watch missed the same-stat content change")
            watching.wait(timeout=20)
            assert watching.returncode == 0
        finally:
            if watching.poll() is None:
                watching.kill()
                watching.wait(timeout=5)

    content = png.read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *values):
            pass
        def do_GET(self):
            if self.path == "/missing.png":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            if self.path == "/slow.png":
                time.sleep(3)
            try:
                self.wfile.write(content)
            except OSError:
                pass
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{http.server_port}"
        web = cli("convert", url + "/wrong.jpg", "--to", "webp", "--output", root / "web")
        assert web["ok"] and len(web["jobs"][0]["outputs"]) == 1
        missing = cli("convert", url + "/missing.png", "--to", "jpg", "--output", root / "failed", expect=1)
        assert not missing["ok"] and not list((root / "failed").glob("*"))
        timed = cli("convert", url + "/slow.png", "--to", "jpg", "--output", root / "timeout", "--timeout", ".5", expect=1)
        assert timed["jobs"][0]["timed_out"] and not list((root / "timeout").glob("*"))
        report.update(web_download="passed", web_http_failure_cleanup="passed", web_timeout_cleanup="passed")
    finally:
        http.shutdown(); http.server_close(); thread.join(2)

    for generation in range(2):
        with (root / f"serve-{generation}.log").open("wb") as log:
            # Each one-file client unpacks its own runtime. Keep the server
            # alive through all sequential clients, including on a busy disk.
            server = subprocess.Popen([str(executable), "serve", "--data-dir", str(profile), "--duration", "120"],
                                      env=environment, cwd=root, stdout=log, stderr=log,
                                      creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            try:
                status = cli("status")
                assert status["ok"]
                accepted = cli("send", png, "--action", "image:jpg", "--output", root / "ipc-output", "--request-id", "standalone-send")
                replay = cli("send", png, "--action", "image:jpg", "--output", root / "ipc-output", "--request-id", "standalone-send")
                assert accepted["job_id"] == replay["job_id"] and replay["replayed"]
                if generation:
                    assert replay["previous_instance"]
                until = time.monotonic() + 15
                while cli("status")["jobs"]:
                    if time.monotonic() > until:
                        raise TimeoutError("IPC job did not complete")
                assert len(list((root / "ipc-output").glob("*.jpg"))) == 1
                cli("stop", "--instance", status["instance"])
                server.wait(timeout=10)
                assert server.returncode == 0
            finally:
                if server.poll() is None:
                    server.kill()
                    server.wait(timeout=5)
    report.update(ipc_queue_and_dedup="passed", ipc_restart="passed", status="passed")
except BaseException as exc:
    report.update(status="failed", error=str(exc))
finally:
    with original.open("rb") as stream:
        report["sha256"] = hashlib.file_digest(stream, "sha256").hexdigest()
    report["artifacts"] = str(root)
    target = Path(args.report).resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
print(json.dumps(report, ensure_ascii=False, indent=2))
raise SystemExit(0 if report["status"] == "passed" else 1)
