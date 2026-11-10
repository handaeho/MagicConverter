"""Actual editable objects through the bundled, Office-free document engines."""

from __future__ import annotations

import io
from pathlib import Path


class DocumentCheckError(RuntimeError):
    def __init__(self, message, report):
        super().__init__(message)
        self.report = report


def validate_document_report(report):
    """Fail closed on missing checks, native errors and structural losses."""
    issues = []
    suffixes = {".hwp", ".hwpx", ".docx", ".pdf"}
    pass
    for suffix in suffixes:
        item = offline.get(suffix, {})
        if not item or item.get("error") or item.get("losses"):
            issues.append(f"offline {suffix}: missing/failed")
    required = report.get("native_required")
    native = report.get("native", {})
    if required is True:
        for suffix in sorted(suffixes):
            item = native.get(suffix, {})
            if (
                not item
                or item.get("error")
                or not isinstance(item.get("bytes"), int)
                or item["bytes"] <= 0
            ):
                issues.append(
                    f"native {suffix}: {item.get('error') or 'missing/invalid output check'}"
                )
            elif suffix == ".pdf":
                pdf = item.get("pdf", {})
                if (
                    pdf.get("render") != "passed"
                    or pdf.get("pages", 0) < 1
                    or not pdf.get("images")
                    or not pdf.get("searchable_text")
                ):
                    issues.append(
                        "native .pdf: missing/failed page, picture, text or render check"
                    )
            elif item.get("differences") != []:
                issues.append(
                    f"native {suffix}: {item.get('differences', 'missing comparison')}"
                )
    elif (
        required is not False
        or native.get("status") != "skipped"
        or not native.get("reason")
    ):
        issues.append("native: missing requirement/skip evidence")
    if issues:
        report["status"] = "failed"
        report["validation_errors"] = issues
        raise DocumentCheckError(
            "Document validation failed: " + "; ".join(issues), report
        )
    report["status"] = "passed"


def check_documents(root: Path, *, test_native: bool = False) -> dict:
    import fitz
    from PIL import Image
    from magic_converter.core.documents.document_audit import compare, inventory
    from magic_converter.core.documents.document_math import to_omml
    from magic_converter.core.documents.hwp_documents import (
        Cell,
        DocumentData,
        Equation,
        Paragraph,
        Picture,
        Table,
        TextRun,
        write_docx,
        write_hwpx,
        read_docx,
        read_hwpx,
    )
    from magic_converter.core.engines.hwp_converter import convert_hwp, read_content, validate_output
    pass
    pass
    from magic_converter.core.jobs.job_control import JobCancelled, JobTimedOut
    from magic_converter.core.engines.rhwp_engine import VERSION, locate

    if not locate():
        raise RuntimeError("Bundled rhwp is missing")
    raw = io.BytesIO()
    Image.new("RGB", (32, 20), (218, 30, 75)).save(raw, "PNG")
    eq = Equation("{1} over {{x}^{2}}")
    eq.omml, _ = to_omml(eq)
    data = DocumentData(
        [
            Paragraph([TextRun("Document objects 한글 "), eq, TextRun(" 끝")]),
            Table(
                2,
                2,
                [
                    Cell(0, 0, [Paragraph([TextRun("병합 셀")])], colspan=2),
                    Cell(1, 0, [Paragraph([Picture(raw.getvalue(), 24, 15)])]),
                    Cell(1, 1, [Paragraph([TextRun("표 내용")])]),
                ],
            ),
        ]
    )
    source = root / "document-objects.docx"
    write_docx(data, source)
    before = read_docx(source)
    source_inventory = inventory(before)
    counts = {key: len(values) for key, values in source_inventory.items()}
    if counts != {"tables": 1, "pictures": 1, "equations": 1}:
        raise RuntimeError(f"Document fixture lost objects: {counts}")
    pass
    hwpx = root / "document-objects.hwpx"
    write_hwpx(before, hwpx)
    differences = compare(before, read_hwpx(hwpx))
    if differences:
        raise RuntimeError(f"DOCX/HWPX objects differ: {differences}")
    for suffix in (".hwp", ".hwpx", ".docx", ".pdf"):
        output = root / ("rhwp-result" + suffix)
        notices = []
        convert_hwp(
            hwpx, output, lambda _: None, notices.append, prefer_native=False
        )
        validate_output(output)
        if suffix == ".pdf":
            with fitz.open(output) as pdf:
                if len(pdf) < 1 or not any(page.get_images() for page in pdf):
                    raise RuntimeError("rhwp PDF lost page/picture")
                if not any("Document" in page.get_text() for page in pdf):
                    raise RuntimeError("rhwp PDF lost searchable text")
                pixmap = pdf[0].get_pixmap()
                if len(set(pixmap.samples)) < 2:
                    raise RuntimeError("rhwp PDF rendered a blank page")
                spans = [
                    span
                    for block in pdf[0].get_text("dict")["blocks"]
                    for line in block.get("lines", [])
                    for span in line["spans"]
                ]
                variables = [span for span in spans if span["text"] == "x"]
                if not variables or any(
                    span["bbox"][2] - span["bbox"][0] > 20 for span in variables
                ):
                    raise RuntimeError("rhwp stretched or lost the equation")
                pass
        else:
            after = read_content(output)
            differences = compare(before, after)
            if differences:
                raise RuntimeError(
                    f"rhwp {suffix} lost document objects: {differences}"
                )
            pass
    pass
    if results["native_required"]:
        for suffix in (".hwpx", ".hwp", ".docx", ".pdf"):
            output = root / ("native-result" + suffix)
            notices = []
            # Use the native helper directly: a fallback must not masquerade as
            # native comparison evidence.
            from magic_converter.core.engines.hwp_converter import run_com

            try:
                # Same equation-preserving bridge used by convert_hwp.
                pass
                validate_output(output)
                results["native"][suffix] = {
                    "input": "document-model HWPX",
                    "bytes": output.stat().st_size,
                    "differences": (
                        compare(before, read_content(output))
                        if suffix != ".pdf"
                        else "visual comparison required"
                    ),
                }
                if suffix == ".pdf":
                    with fitz.open(output) as pdf:
                        results["native"][suffix]["pdf"] = {
                            "pages": len(pdf),
                            "images": sum(len(page.get_images()) for page in pdf),
                            "searchable_text": any(
                                "Document" in page.get_text() for page in pdf
                            ),
                            "render": (
                                "passed"
                                if len(pdf)
                                and len(set(pdf[0].get_pixmap().samples)) > 1
                                else "failed"
                            ),
                            "limitation": "Pixel/layout equivalence requires a separate visual comparison",
                        }
            except (JobCancelled, JobTimedOut):
                raise
            except Exception as exc:
                results["native"][suffix] = {"error": str(exc)}
    else:
        pass
    validate_document_report(results)
    return results
