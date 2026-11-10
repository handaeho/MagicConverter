"""Portable checks for watch recovery, HTTP helpers, CLI and current-user IPC."""
from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from dataclasses import asdict
from pathlib import Path


def check_plain_text_urls():
    from PySide6.QtCore import QMimeData
    from magic_converter.core.automation.web_inputs import from_mime
    url = 'https://example.invalid/a%20b.png?key=a:b#part'
    mime = QMimeData(); mime.setText(url)
    paths, items = from_mime(mime)
    if len(items) != 1 or items[0].url != url or paths != (items[0].placeholder,):
        raise RuntimeError('Single text/plain URL was not preserved')
    refused = ['Article ' + url, url + '\n' + url, 'file:///C:/Windows/a.png',
               'https://user:pass@example.invalid/a.png', 'https://example.invalid:bad/a']
    for value in refused:
        mime = QMimeData(); mime.setText(value)
        if from_mime(mime) != ((), ()):
            raise RuntimeError('Unsafe or selected plain text was accepted')
    mime = QMimeData(); mime.setText(url); mime.setHtml('<p>Body</p><img src="' + url + '">')
    if from_mime(mime) != ((), ()):
        raise RuntimeError('Plain URL bypassed selected HTML refusal')
    return {'accepted': 1, 'refused': len(refused) + 1}


def check_automation(app, root: Path, image: Path):
    from PySide6.QtCore import QMimeData
    from magic_converter.core.automation.web_inputs import from_mime
    plain_text_urls = check_plain_text_urls()
    for kind, value in [('text/x-moz-url', 'https://example.invalid/a.png\nTitle'),
                        ('DownloadURL', 'image/png:a.png:https://example.invalid/a.png')]:
        mime = QMimeData(); mime.setData(kind, value.encode('utf-8'))
        if len(from_mime(mime)[1]) != 1:
            raise RuntimeError('Browser MIME adapter failed: ' + kind)
        mime.setHtml('<p>Selected body</p><img src="https://example.invalid/a.png">')
        if from_mime(mime) != ((), ()):
            raise RuntimeError('HTML body selection was treated as an image')
    from PySide6.QtGui import QImage
    mime = QMimeData(); mime.setImageData(QImage(str(image)))
    mime.setHtml('<img src="blob:https://example.invalid/image">')
    mime.setData('text/x-moz-url', b'blob:https://example.invalid/image\nTitle')
    if not from_mime(mime)[1] or not from_mime(mime)[1][0].data:
        raise RuntimeError('Image-only blob drag lost its supplied pixels')
    mime.setHtml('<p>Selected article</p><img src="blob:https://example.invalid/image">')
    if from_mime(mime) != ((), ()):
        raise RuntimeError('Selected article bypassed refusal using image pixels')
    mime = QMimeData(); mime.setImageData(QImage(str(image)))
    mime.setHtml('<img src="https://example.invalid/animation.gif">')
    if from_mime(mime)[1][0].url != 'https://example.invalid/animation.gif':
        raise RuntimeError('HTTP animation was replaced by thumbnail pixels')
    from PIL import Image
    from magic_converter.core.automation.automation_store import AutomationStore, WatchRule
    from magic_converter.core.conversion.converter import convert_image
    from magic_converter.core.settings.json_store import read_json
    from magic_converter.core.automation.local_ipc import call
    from magic_converter.core.settings.preferences import Preset, OutputRule, default_preferences
    from magic_converter.core.jobs.process_runner import run_process, start_process, stop_process
    from magic_converter.core.automation.watch_engine import WatchEngine
    from magic_converter.core.automation.web_inputs import from_url
    from magic_converter.core.automation.web_transfer import materialize

    work = root / "automation"
    work.mkdir()
    inbox = work / "inbox"
    inbox.mkdir()
    source = inbox / "감시 파일.png"
    source.write_bytes(image.read_bytes())
    recipe = Preset("portable-jpg", "JPG", "image:jpg")
    rule = WatchRule("portable", str(inbox), str(inbox / "_converted"), recipe, settle=.1)
    state = work / "watch-state"
    engine = WatchEngine(state, (rule,))
    engine.scan(0)
    event = engine.scan(.2)[0][1]
    converted = Path(event["work"]) / "results" / "image.jpg"
    convert_image(source, converted, ".jpg")
    target = engine.finish(event, True, [converted])
    restarted = WatchEngine(state, (rule,))
    restarted.recover()
    restarted.scan(0)
    if restarted.scan(.2) or not restarted.receipt_valid(target, event["key"]):
        raise RuntimeError("Watch restart produced a duplicate")
    prefix = [sys.executable] if getattr(sys, "frozen", False) else [sys.executable, str(Path(__file__).resolve().parents[3] / "run_magic_converter.py")]
    cli_report = work / "cli-convert.json"
    result = run_process([*prefix, "convert", "--action", "image:webp", str(image), "--output", str(work / "cli-output"),
                          "--data-dir", str(work / "cli-data"), "--report", str(cli_report)], timeout=30)
    if result.returncode or not read_json(cli_report).get("ok"):
        raise RuntimeError("Portable CLI conversion failed: " + result.stderr[-800:])

    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    content = image.read_bytes()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_GET(self):
            if self.path == "/missing.png":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(content)))
            self.end_headers()
            self.wfile.write(content)
    http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    try:
        address = f"http://127.0.0.1:{http.server_port}"
        copied = QMimeData(); copied.setText(address + "/image.jpg")
        fetched = materialize(from_mime(copied)[1][0], work / "download" / "input", lambda _: None)
        with Image.open(fetched) as decoded:
            if decoded.size != (64, 48) or fetched.suffix != ".png":
                raise RuntimeError("Web content sniffing failed")
        try:
            materialize(from_url(address + "/missing.png"), work / "failed-download" / "input", lambda _: None)
        except Exception:
            pass
        else:
            raise RuntimeError("HTTP failure was not reported")
        if list((work / "failed-download" / "input").glob("*")):
            raise RuntimeError("Failed HTTP transfer left files behind")
    finally:
        http.shutdown()
        http.server_close()
        thread.join(2)

    profile = work / "ipc-profile"
    request = {"id": "portable-send", "command": "send", "paths": [str(image)],
               "recipe": asdict(Preset("ipc-jpg", "JPG", "image:jpg", output=OutputRule("folder", str(work / "ipc-output"))))}
    first_instance = None
    for generation in range(2):
        log = work / f"ipc-{generation}.log"
        with log.open("wb") as stream:
            process, job = start_process([*prefix, "serve", "--duration", "25", "--data-dir", str(profile)], stdout=stream, stderr=stream)
            try:
                status = call(profile, {"command": "status"}, timeout=12)
                if not status.get("ok"):
                    raise RuntimeError("IPC status failed")
                instance = status["instance"]
                response = call(profile, request)
                replay = call(profile, request)
                if not response.get("ok") or not replay.get("replayed"):
                    raise RuntimeError("IPC request deduplication failed")
                if generation:
                    if instance == first_instance or not replay.get("previous_instance"):
                        raise RuntimeError("IPC restart isolation failed")
                    stale = call(profile, {"command": "cancel", "instance": first_instance, "job_id": response["job_id"]})
                    if stale.get("ok"):
                        raise RuntimeError("Stale IPC cancel accepted after restart")
                deadline = time.monotonic() + 10
                while call(profile, {"command": "status"})["jobs"]:
                    if time.monotonic() > deadline:
                        raise RuntimeError("IPC conversion did not finish")
                    time.sleep(.02)
                if len(list((work / "ipc-output").glob("*.jpg"))) != 1:
                    raise RuntimeError("IPC replay published a duplicate")
                first_instance = first_instance or instance
                call(profile, {"command": "stop", "instance": instance})
                process.wait(timeout=8)
                if process.returncode:
                    raise RuntimeError("IPC server failed: " + log.read_text(encoding="utf-8", errors="replace")[-800:])
            finally:
                stop_process(process, job)

    from magic_converter.ui.automation.input_dialog import InputDialog
    from magic_converter.ui.automation.automation_dialog import AutomationDialog
    from magic_converter.core.automation.watch_controller import WatchController
    from magic_converter.core.conversion.conversion_service import ConversionService
    store = AutomationStore(work / "dialog-state")
    store.save((rule,), str(work / "web-output"))
    service = ConversionService()
    controller = WatchController(service, store)
    inputs = InputDialog([image], default_preferences(), service.ffmpeg)
    dialog = AutomationDialog(controller, default_preferences())
    if inputs.grab().isNull() or dialog.grab().isNull():
        raise RuntimeError("Automation/Explorer input UI render failed")
    inputs.reject(); dialog.reject()
    inputs.deleteLater(); dialog.deleteLater()
    controller.stop()
    app.processEvents()
    return {"watch_stable_publish": "passed", "watch_restart_dedup": "passed", "cli_conversion": "passed",
            "web_helper_and_actual_format": "passed", "web_failed_cleanup": "passed",
            "ipc_send_replay": "passed", "ipc_restart_and_stale_cancel": "passed", "automation_input_ui": "passed",
            "browser_mime_and_selection": "passed", "plain_text_urls": plain_text_urls}
