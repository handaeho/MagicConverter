"""Office template/macro inputs become macro-free standard OOXML packages.

No Office process is started by normalization. VBA, signatures, ActiveX and
embedded executable objects are removed; remaining unsupported objects are
reported by the document model rather than executed.
"""
from __future__ import annotations

import zipfile
from pathlib import Path, PurePosixPath
from xml.etree import ElementTree as ET

from magic_converter.core.jobs.job_control import check_cancelled

WORD = frozenset({".docx", ".docm", ".dotx", ".dotm"})
SLIDES = frozenset({".pptx", ".pptm", ".potx", ".potm", ".ppsx", ".ppsm"})
SHEETS = frozenset({".xlsx", ".xlsm", ".xltx", ".xltm"})
OFFICE = WORD | SLIDES | SHEETS
VARIANTS = OFFICE - {".docx", ".pptx", ".xlsx"}
TYPES = {".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml",
         ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
         ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"}


def canonical(suffix: str) -> str:
    return ".docx" if suffix in WORD else ".pptx" if suffix in SLIDES else ".xlsx" if suffix in SHEETS else suffix


def normalize(source: Path, destination: Path) -> None:
    from magic_converter.core.conversion.converter import ConversionError
    target = canonical(source.suffix.lower())
    part = {".docx": "/word/document.xml", ".pptx": "/ppt/presentation.xml", ".xlsx": "/xl/workbook.xml"}[target]
    blocked = ("vbaproject", "vbadata", "activex", "embeddings/", "_xmlsignatures/")
    with zipfile.ZipFile(source) as original, zipfile.ZipFile(destination, "w", zipfile.ZIP_DEFLATED) as output:
        names = original.namelist()
        if len(names) != len(set(names)) or part[1:] not in names or "[Content_Types].xml" not in names:
            raise ConversionError("Office 패키지의 필수 구성 요소가 없거나 중복됩니다.")
        if sum(i.file_size for i in original.infolist()) > 512 * 1024 * 1024:
            raise ConversionError("Office 패키지의 압축 해제 크기가 너무 큽니다.")
        removed_ids, relationship_xml = {}, {}
        def xml(data):
            if b"<!DOCTYPE" in data.upper() or b"<!ENTITY" in data.upper():
                raise ConversionError("외부 엔터티를 포함한 Office XML은 읽지 않습니다.")
            return ET.fromstring(data)
        for name in names:
            if not name.endswith(".rels") or any(value in name.lower() for value in blocked):
                continue
            check_cancelled()
            node = xml(original.read(name))
            relationship = PurePosixPath(name)
            owner = str(relationship.parent.parent / relationship.name[:-5])
            removed = set()
            for child in list(node):
                if any(item in " ".join(child.attrib.values()).lower() for item in blocked):
                    removed.add(child.get("Id"))
                    node.remove(child)
            removed_ids[owner] = removed
            relationship_xml[name] = ET.tostring(node, encoding="utf-8", xml_declaration=True)
        def remove_references(parent, removed):
            for child in list(parent):
                tag = child.tag.rsplit("}", 1)[-1]
                references = any(value in removed and key.startswith("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}")
                                 for key, value in child.attrib.items())
                object_reference = tag in {"object", "oleObj", "oleObject", "control", "graphicFrame"} and any(
                    value in removed and key.startswith("{http://schemas.openxmlformats.org/officeDocument/2006/relationships}")
                    for node in child.iter() for key, value in node.attrib.items())
                if references or object_reference:
                    parent.remove(child)
                else:
                    remove_references(child, removed)
                    if tag in {"oleObjects", "controls"} and not len(child):
                        parent.remove(child)
        for info in original.infolist():
            check_cancelled()
            if any(value in info.filename.lower() for value in blocked):
                continue
            data = relationship_xml.get(info.filename)
            if data is None:
                data = original.read(info)
            if info.filename == "[Content_Types].xml" or info.filename.endswith(".rels"):
                node = xml(data)
                for child in list(node):
                    value = " ".join(child.attrib.values()).lower()
                    if any(item in value for item in blocked):
                        node.remove(child)
                    elif child.get("PartName") == part:
                        child.set("ContentType", TYPES[target])
                data = ET.tostring(node, encoding="utf-8", xml_declaration=True)
            elif removed_ids.get(info.filename):
                node = xml(data)
                remove_references(node, removed_ids[info.filename])
                data = ET.tostring(node, encoding="utf-8", xml_declaration=True)
            output.writestr(info.filename, data)
