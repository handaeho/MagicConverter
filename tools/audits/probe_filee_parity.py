"""Bounded synthetic audit probes. Records findings without changing the app.

These are comparison/negative probes, not regression tests that assert today's
defects are desirable. No real user data, network transfers or shell registration.
"""

import argparse
import io
import json
import os
import random
import subprocess
import sys
import tempfile
import zipfile
from unittest.mock import patch
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--report", type=Path, required=True)
    args = parser.parse_args()
    from PIL import Image
    from PySide6.QtCore import QMimeData
    from PySide6.QtWidgets import QApplication
    from magic_converter.core.specialized.data_converter import read_data, convert as convert_data
    from magic_converter.core.specialized.ebook_converter import (
        Book,
        Chapter,
        write_epub,
        read_book,
    )
    from magic_converter.core.specialized.safe_html import image_uri
    from magic_converter.core.specialized.geo_converter import read_geo
    from magic_converter.core.automation.web_inputs import from_mime
    from magic_converter.core.automation.automation_store import WatchRule
    from magic_converter.core.automation.watch_engine import WatchEngine
    from magic_converter.core.settings.preferences import Preset
    from magic_converter.core.conversion.builtin_converters import create_catalog
    from magic_converter.core.conversion.file_router import slots_for
    from magic_converter.core.conversion.conversion_request import prepare_request
    from magic_converter.core.conversion.conversion_execution import execute_file

    app = QApplication.instance() or QApplication([])
    findings = []

    def record(identity, finding, **evidence):
        findings.append(
            {"id": identity, "finding_reproduced": finding, "evidence": evidence}
        )

    with tempfile.TemporaryDirectory(prefix="magic-parity-probes-") as directory:
        root = Path(directory)
        source = root / "business.xml"
        source.write_text(
            '<magic-data version="1"><customer id="42">Alice</customer></magic-data>',
            encoding="utf-8",
        )
        result = read_data(source, "xml")
        record(
            "XML_ENVELOPE_CHILD_LOSS",
            result is None,
            input=source.read_text(),
            parsed=result,
            expected="Normal XML envelope preserving customer, or explicit refusal; never silent null",
        )

        kml = root / "attributes.kml"
        kml.write_text(
            '<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Schema id="s"><SimpleField name="population" type="int"/></Schema><Placemark><ExtendedData><SchemaData schemaUrl="#s"><SimpleData name="population">42</SimpleData></SchemaData></ExtendedData><Point><coordinates>126,37</coordinates></Point></Placemark></Document></kml>',
            encoding="utf-8",
        )
        notices = []
        features = read_geo(kml, "kml", notices.append)
        record(
            "KML_SCHEMA_DATA_LOSS",
            "population" not in features[0]["properties"],
            properties=features[0]["properties"],
            notices=notices,
            expected="Preserve SchemaData/SimpleData attributes or explicitly report their exclusion",
        )

        inbox = root / "inbox"
        inbox.mkdir()
        watched = inbox / "data.json"
        watched.write_bytes(b'{"x":1}')
        rule = WatchRule(
            "probe",
            str(inbox),
            str(root / "out"),
            Preset("yaml", "YAML", "data:yaml"),
            settle=0.1,
        )
        engine = WatchEngine(root / "watch", (rule,))
        engine.scan(0)
        ready = engine.scan(0.2)
        assert len(ready) == 1
        event = ready[0][1]
        result_path = Path(event["work"]) / "results" / "data.yaml"
        result_path.write_text("x: 1\n", encoding="utf-8")
        assert engine.finish(event, True, [result_path])
        engine.cleanup(event)
        original_stat = watched.stat()
        watched.write_bytes(b'{"x":2}')
        os.utime(watched, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
        after_change = []
        for tick in range(21):
            after_change.extend(engine.scan(0.4 + tick * 0.5))
        restarted = WatchEngine(root / "watch", (rule,))
        restarted.scan(12)
        after_restart = restarted.scan(12.2)
        record(
            "WATCH_EQUAL_STAT_CHANGE_MISSED",
            not after_change and bool(after_restart),
            next_events=len(after_change),
            elapsed_scan_window_seconds=10,
            events_after_restart=len(after_restart),
            same_size=watched.stat().st_size == original_stat.st_size,
            same_mtime=watched.stat().st_mtime_ns == original_stat.st_mtime_ns,
            expected="Changed content is eventually detected without restarting when timestamps are preserved",
        )
        for _, pending in after_restart:
            restarted.cleanup(pending)

        mime = QMimeData()
        mime.setData(
            "text/x-moz-url",
            "https://example.invalid/a.png\nPicture".encode("utf-16-le"),
        )
        paths, web = from_mime(mime)
        record(
            "WEB_MOZ_URL_FALLBACK_MISSING",
            not paths and not web,
            formats=mime.formats(),
            input_kind="Synthetic QMimeData with only text/x-moz-url; not an actual browser OLE test",
        )
        mime = QMimeData()
        mime.setHtml(
            '<p>Selected article text<img src="https://example.invalid/tracker.png"/></p>'
        )
        paths, web = from_mime(mime)
        record(
            "WEB_HTML_TEXT_SELECTION_TAKES_IMAGE",
            bool(web),
            selected_url=web[0].url if web else None,
            expected="Distinguish an image-only drag from selected article HTML containing an image",
        )

        catalog = create_catalog()
        record(
            "GIF_PNG_CONVERT_ROUTE_MISSING",
            catalog.assess(Path("animation.gif"), "image:png").route is None,
            wheel_actions=[
                s.action.key for s in slots_for(["animation.gif"], "convert")
            ],
            note="Frame extraction exists as a tool; ordinary GIF to PNG conversion is not routed",
        )
        source = root / "single.json"
        source.write_text('{"value":42}', encoding="utf-8")
        negative_csv = root / "table.csv"
        try:
            convert_data(source, negative_csv, "json", "csv", lambda _: None)
            refused = False
        except Exception:
            refused = True
        record("DATA_NESTED_REFUSAL_CONTROL", False, expected_refusal_passed=refused)

        epub = root / "book.epub"
        write_epub(
            Book("Probe", chapters=[Chapter("One", "<p>Visible chapter</p>")]), epub
        )
        with zipfile.ZipFile(epub, "a") as package:
            package.writestr("META-INF/sinf.xml", "<sinf/>")
        try:
            accepted = len(read_book(epub, "epub").chapters) == 1
        except Exception:
            accepted = False
        record(
            "EPUB_SINF_MARKER_ACCEPTED",
            accepted,
            note="Marker-only synthetic file; no encrypted payload or decryption. Upstream checks this marker too.",
        )

        image_buffer = io.BytesIO()
        pixels = random.Random(19).randbytes(128 * 128 * 3)
        Image.frombytes("RGB", (128, 128), pixels).save(image_buffer, format="PNG")
        image = image_buffer.getvalue()
        uri = image_uri(image)
        amplified = root / "references.epub"
        write_epub(
            Book(
                "Reference expansion",
                chapters=[Chapter("One", "<p>" + f'<img src="{uri}"/>' * 32 + "</p>")],
            ),
            amplified,
        )
        book = read_book(amplified, "epub")
        body_bytes = sum(len(ch.body.encode("utf-8")) for ch in book.chapters)
        record(
            "EPUB_ASSET_REFERENCE_AMPLIFICATION",
            body_bytes > amplified.stat().st_size * 20,
            package_bytes=amplified.stat().st_size,
            embedded_image_bytes=len(image),
            references=32,
            expanded_model_body_bytes=body_bytes,
            unique_asset_bytes=book.assets.byte_size,
            unique_assets=len(book.assets),
            total_model_payload_bytes=body_bytes + book.assets.byte_size,
            ratio=round(body_bytes / amplified.stat().st_size, 2),
            note="Bounded repeated-reference probe; body and unique asset bytes are reported separately. Not a process-RSS measurement.",
        )
        from magic_converter.core.engines.rhwp_engine import locate

        if locate():
            from magic_converter.core.diagnostics.document_diagnostics import (
                check_documents,
                DocumentCheckError,
            )

            native_root = root / "native-gate"
            native_root.mkdir()
            pass
            errors = {
                kind: value.get("error")
                for kind, value in result["native"].items()
                if isinstance(value, dict) and value.get("error")
            }
            record(
                "NATIVE_COMPARISON_ERROR_NOT_RAISED",
                len(errors) == 4 and returned_normally,
                native_errors=errors,
                returned_normally=returned_normally,
                note="Offline rhwp checks are real. Only the native comparison call is fault-injected; no COM app is launched by this probe. A strict gate must raise and retain all native error details.",
            )
        app.processEvents()

    commit = subprocess.check_output(
        ["git", "-C", str(ROOT), "rev-parse", "HEAD"], text=True
    ).strip()
    report = {
        "local_commit": commit,
        "note": "Reproductions and negative controls; a true finding is an outstanding issue/compatibility boundary, not a passing product assertion.",
        "probes": findings,
    }
    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
