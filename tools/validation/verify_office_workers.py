"""Frozen Office broker and HWP/HWPX library route evidence."""

import argparse
import hashlib
import json
import os
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from docx import Document
from openpyxl import Workbook
from pptx import Presentation
from pypdf import PdfReader
from magic_converter import __version__
from magic_converter.core.documents.hwp_documents import read_hwpx
from magic_converter.core.engines.office_com import com_available


def sha(path):
    with path.open("rb") as stream: return hashlib.file_digest(stream, "sha256").hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--executable", type=Path, required=True)
    parser.add_argument("--gui-executable", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--office", action="store_true")
    args = parser.parse_args()
    folder = args.output.resolve(); folder.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env["PATH"] = str(Path(os.environ.get("SystemRoot", "C:/Windows")) / "System32")
    env["PYTHONUTF8"] = "1"
    for key in ("PYTHONPATH", "PYTHONHOME", "VIRTUAL_ENV", "MAGIC_CONVERTER_FFMPEG", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        env.pop(key, None)
    cli, gui = args.executable.resolve(), args.gui_executable.resolve()
    source = folder / "document.docx"
    doc = Document(); doc.add_paragraph("Office worker text"); doc.save(source)
    workbook = Workbook(); workbook.active.append(["Office worker sheet", 42])
    sheet = folder / "sheet.xlsx"; workbook.save(sheet); workbook.close()
    deck = Presentation(); deck.slides.add_slide(deck.slide_layouts[5]).shapes.title.text = "Office worker slide"
    slides = folder / "slides.pptx"; deck.save(slides)
    originals = {p: sha(p) for p in (source, sheet, slides)}

    def convert(path, action, label, *, executable=cli, success=True):
        report = folder / (label + ".json")
        process = subprocess.run([str(executable), "convert", str(path), "--action", action,
                   "--output", str(folder / label), "--data-dir", str(folder / "profile"),
                   "--timeout", "60", "--report", str(report), "--json"], env=env,
                   capture_output=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        result = json.loads(report.read_text(encoding="utf-8-sig"))
        assert process.returncode == (0 if success else 1) and result["ok"] == success, result
        if not success:
            assert not list((folder / label).glob("*"))
            return result
        return Path(result["jobs"][0]["outputs"][0]), result["jobs"][0]

    hwpx, job = convert(source, "document:hwpx", "hwpx")
    assert "Office worker text" in read_hwpx(hwpx).text()
    assert any("문서 모델" in notice for notice in job["warnings"])
    hwp, _ = convert(hwpx, "document:hwp", "binary")
    restored, _ = convert(hwp, "document:docx", "restored")
    assert "Office worker text" in "\n".join(p.text for p in Document(restored).paragraphs)
    windowed, _ = convert(source, "document:hwpx", "windowed", executable=gui)
    assert "Office worker text" in read_hwpx(windowed).text()
    invalid = folder / "invalid.hwpx"; invalid.write_bytes(b"invalid zip")
    convert(invalid, "document:docx", "invalid", success=False)
    empty = folder / "empty.zip"
    with zipfile.ZipFile(empty, "w"): pass
    extracted, _ = convert(empty, "tool:archive_extract", "extract")
    assert extracted.is_dir() and not list(extracted.iterdir())
    subtitle = folder / "captions.srt"; subtitle.write_text("1\n00:00:00,000 --> 00:00:01,000\nHello\n", encoding="utf-8")
    exported, _ = convert(subtitle, "subtitle:vtt", "subtitles")
    assert "Hello" in exported.read_text(encoding="utf-8-sig")
    native = {"status": "skipped", "reason": "--office not selected (CI has no installed Office)"}
    if args.office:
        records = []
        for path, progid, text in ((source, "Word.Application", "Office worker text"),
                                   (sheet, "Excel.Application", "Office worker sheet"),
                                   (slides, "PowerPoint.Application", "Office worker slide")):
            assert com_available(progid), progid
            output, job = convert(path, "document:pdf", "native-" + path.suffix[1:])
            reader = PdfReader(output)
            assert text in "\n".join(p.extract_text() for p in reader.pages)
            assert any(n.startswith("PDF 구조 검사:") for n in job["warnings"])
            assert not any("실패" in n or "완료하지 못" in n or "대체" in n for n in job["warnings"]), job
            records.append({"source": path.name, "pages": len(reader.pages), "metadata": dict(reader.metadata or {}),
                            "warnings": job["warnings"]})
        output, job = convert(source, "document:pdf", "windowed-native", executable=gui)
        assert "Office worker text" in "\n".join(p.extract_text() for p in PdfReader(output).pages)
        assert not any("실패" in n or "완료하지 못" in n or "대체" in n for n in job["warnings"]), job
        native = {"status": "passed", "exports": records, "windowed_word": "passed"}
    assert all(sha(p) == digest for p, digest in originals.items())
    result = {"status": "passed", "version": __version__, "cli_sha256": sha(cli), "gui_sha256": sha(gui),
              "rhwp_roundtrip": "passed", "windowed_worker_events": "passed",
              "invalid_input": "refused_without_publication", "empty_archive": "passed", "subtitle": "passed",
              "source_hashes_preserved": True, "native_office": native}
    (folder / "report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__": main()
