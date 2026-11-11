"""Structural fidelity evidence, independent of the selected document engine."""
from __future__ import annotations

import hashlib
from collections import Counter

from magic_converter.core.documents.hwp_documents import DocumentData, Equation, InlineLine, Picture, Table, Paragraph, TextRun
from magic_converter.core.documents.rich_hwp import walk_blocks


def hwp_object_counts(source) -> dict[str, int]:
    """Count original HWP records without passing through rhwp's exporter."""
    import olefile
    import struct
    from magic_converter.core.documents.hwp_documents import MAX_XML
    from magic_converter.core.documents.rich_hwp import _inflate, records
    from magic_converter.core.conversion.converter import ConversionError
    from magic_converter.core.jobs.job_control import check_cancelled
    result = {"tables": 0, "pictures": 0, "equations": 0}
    with olefile.OleFileIO(source) as archive:
        header = archive.openstream("FileHeader").read()
        flags = struct.unpack_from("<I", header, 36)[0]
        for name in archive.listdir():
            if name[0] != "BodyText":
                continue
            check_cancelled()
            if archive.get_size(name) > MAX_XML:
                raise ConversionError("HWP 본문 스트림이 너무 큽니다.")
            raw = archive.openstream(name).read()
            if flags & 1:
                raw = _inflate(raw)
            for node in records(raw):
                key = {77: "tables", 85: "pictures", 88: "equations"}.get(node.tag)
                if key:
                    result[key] += 1
    return result


def inventory(data: DocumentData) -> dict:
    objects = list(walk_blocks([*data.headers, *data.blocks, *data.footers]))
    return {
        "tables": [tuple([obj.rows, obj.cols, *sorted((c.row, c.col, c.rowspan, c.colspan) for c in obj.cells)])
                   for obj in objects if isinstance(obj, Table)],
        "pictures": [hashlib.sha256(obj.data).hexdigest() for obj in objects if isinstance(obj, Picture)],
        "lines": [(round(obj.right - obj.left, 2), round(obj.stroke, 2), obj.color)
                  for obj in objects if isinstance(obj, InlineLine)],
        "equations": ["".join(obj.script.split()) for obj in objects if isinstance(obj, Equation)],
    }


def compare(before: DocumentData, after: DocumentData) -> list[str]:
    a, b = inventory(before), inventory(after)
    warnings = []
    for key, label in (("tables", "표·병합 구조"), ("pictures", "그림 데이터"), ("lines", "선 도형"), ("equations", "수식 구조")):
        lost = sum((Counter(a[key]) - Counter(b[key])).values())
        if lost:
            warnings.append(f"손실 검사: {label} {lost}개가 원본과 다릅니다 (원본 {len(a[key])}개 / 결과 {len(b[key])}개).")
    source_text = "".join(before.text().split())
    output_text = "".join(after.text().split())
    if source_text and source_text != output_text:
        warnings.append("손실 검사: 본문·셀·수식 텍스트가 원본과 다릅니다.")
    def metrics(data):
        from magic_converter.core.documents.character_spacing import metric_characters
        result = []
        explicit = False
        for obj in walk_blocks([*data.headers, *data.blocks, *data.footers]):
            if not isinstance(obj, Paragraph):
                continue
            paragraph = []
            for run in obj.runs:
                if not isinstance(run, TextRun) or not run.text:
                    continue
                explicit |= bool(run.spacing_percent or run.spacing_pt or run.width_percent != 100)
                paragraph.extend(metric_characters(run))
            result.append(paragraph)
        return result, explicit
    source_metrics, explicit = metrics(before)
    output_metrics, output_explicit = metrics(after)
    def equal_metrics(a, b):
        if len(a) != len(b):return False
        for first, second in zip(a,b):
            if len(first) != len(second):return False
            accumulated=0
            for x,y in zip(first,second):
                if x[:2] != y[:2] or x[3] != y[3] or abs(x[2]-y[2]) > (.051 if x[1]=='pt'else 1e-8):return False
                if x[1]=='pt':
                    accumulated+=x[2]-y[2]
                    if abs(accumulated)>.1:return False
        return True
    if (explicit or output_explicit) and not equal_metrics(source_metrics, output_metrics):
        warnings.append('손실 검사: 명시된 자간·장평이 원본과 다릅니다. 글꼴별 실제 간격·조판 동일성을 보장하지 않습니다.')
    from magic_converter.core.documents.paragraph_layout import FIELDS
    def layouts(data):
        return [(obj.text, tuple(getattr(obj, name) for name in FIELDS))
                for obj in walk_blocks([*data.headers, *data.blocks, *data.footers]) if isinstance(obj, Paragraph)]
    original, returned = layouts(before), layouts(after)
    if any(any(values) for _, values in original + returned):
        if (len(original) != len(returned) or any(a[0] != b[0] or any(abs(x-y) > .051 for x, y in zip(a[1], b[1]))
                                                for a, b in zip(original, returned))):
            warnings.append('손실 검사: 문단 들여쓰기·앞뒤 간격이 원본과 다릅니다.')
    return list(dict.fromkeys([*before.warnings, *after.warnings, *warnings]))
