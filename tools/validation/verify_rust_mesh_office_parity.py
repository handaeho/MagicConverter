"""Run the native and pinned Python entry points on mesh and OOXML fixtures."""

import argparse
import json
from pathlib import Path
import struct
import sys
from time import perf_counter
from xml.etree import ElementTree as ET
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run

SLIDES = "http://schemas.openxmlformats.org/presentationml/2006/main"
REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
TYPES = "http://schemas.openxmlformats.org/package/2006/content-types"
RELS = "http://schemas.openxmlformats.org/package/2006/relationships"


def package_fixture(path):
    part = {"docx": "word/document.xml", "xlsx": "xl/workbook.xml"}.get(path.suffix[1:], "ppt/presentation.xml")
    with ZipFile(path, "w") as archive:
        archive.writestr("[Content_Types].xml", f'<Types xmlns="{TYPES}"><Override PartName="/{part}" ContentType="old-type"/><Override PartName="/ppt/vbaProject.bin" ContentType="vbaProject"/></Types>')
        archive.writestr(part, f'<p:body xmlns:p="{SLIDES}" xmlns:r="{REL}"><p:graphicFrame><p:oleObj r:id="rId1"/></p:graphicFrame><p:text>Keep</p:text></p:body>')
        folder, file = part.split("/")
        archive.writestr(f"{folder}/_rels/{file}.rels", f'<Relationships xmlns="{RELS}"><Relationship Id="rId1" Target="embeddings/private.bin" Type="{REL}/oleObject"/><Relationship Id="rId2" Target="media/keep.png" Type="{REL}/image"/></Relationships>')
        archive.writestr(f"{folder}/embeddings/private.bin", b"embedding")
        archive.writestr(f"{folder}/vbaProject.bin", b"macro")
        archive.writestr(f"{folder}/media/keep.png", b"keep")
        archive.writestr("docProps/core.xml", '<core><creator>Private</creator></core>')
        archive.writestr("docProps/custom.xml", '<custom><secret>Private</secret></custom>')
        archive.writestr("docProps/app.xml", '<app><Company>Private</Company><Manager>Private</Manager><Application>Keep</Application></app>')


def xml_value(element):
    return [element.tag, sorted(element.attrib.items()), (element.text or "").strip(),
            [xml_value(child) for child in element]]


def package_model(path):
    with ZipFile(path) as archive:
        return {name: (xml_value(ET.fromstring(archive.read(name))) if name.endswith((".rels", ".xml"))
                       else digest_value(archive.read(name))) for name in archive.namelist()}


def digest_value(value):
    import hashlib
    return hashlib.sha256(value).hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    evidence = {"worker_sha256": digest(args.worker), "cases": {}}
    baseline = args.baseline.resolve()
    sys.path.insert(0, str(baseline))
    from magic_converter.core.specialized.mesh_converter import read_mesh

    def mesh_model(path):
        mesh = read_mesh(path, path.suffix[1:])
        return [[list(vertex) for vertex in mesh.vertices], [list(face) for face in mesh.faces]]

    source = root / "inputs"
    source.mkdir()
    obj = source / "한글.obj"
    obj.write_text("v 0 0 0\nv 2 0 0\nv 2 2 0\nv 1 1 0\nv 0 2 0\nf -5 -4 -3 -2 -1\n", encoding="utf-8")
    stl = source / "한글.stl"
    stl.write_bytes(bytes(80) + struct.pack("<I12fH", 1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 1, 0, 0))
    ply = source / "한글.ply"
    ply.write_bytes(b"ply\nformat binary_big_endian 1.0\nelement vertex 3\nproperty float x\nproperty float y\nproperty float z\nproperty uchar red\nelement face 1\nproperty list uchar int vertex_indices\nend_header\n"
                    + b"".join(struct.pack(">fffB", *v) for v in [(0, 0, 0, 1), (1, 0, 0, 2), (0, 1, 0, 3)])
                    + struct.pack(">Biii", 3, 0, 1, 2))
    cases = [(p, f"mesh:{target}", mesh_model, 0) for p in (obj, stl, ply)
             for target in ("obj", "stl", "ply") if p.suffix[1:] != target]
    crossing = source / "crossing.obj"
    crossing.write_text("v 0 0 0\nv 1 1 0\nv 0 1 0\nv 1 0 0\nf 1 2 3 4\n", encoding="utf-8")
    cases.append((crossing, "mesh:ply", mesh_model, 1))
    for extension in ("docx", "pptx", "xlsx", "pptm", "potx", "potm", "ppsx", "ppsm"):
        path = source / f"OOXML.{extension}"
        package_fixture(path)
        if extension in ("docx", "pptx", "xlsx"):
            cases.append((path, "strip", package_model, 0))
        if extension not in ("docx", "xlsx"):
            cases.append((path, "office:pptx", package_model, 0))
    try:
        for path, action, model, expected in cases:
            key = f"{path.name}-{action.replace(':', '-')}"
            outputs = []
            values = []
            for name, prefix, environment in (
                ("python", [sys.executable, "-B", baseline / "run_magic_converter.py"], None),
                ("rust", [args.worker.resolve()], native_environment()),
            ):
                output = root / key / name
                output.mkdir(parents=True)
                start = perf_counter()
                code, report = run(prefix, ("convert", path, "--action", action, "--output", output,
                                            "--data-dir", root / f"profile-{name}", "--json"), environment=environment)
                outputs.append({"code": code, "s": perf_counter() - start, "report": report})
                values.append([model(p) for p in sorted(output.iterdir())] if code == 0 else list(output.iterdir()))
            evidence["cases"][key] = {
                "equal": all(x["code"] == expected for x in outputs) and values[0] == values[1],
                "python": outputs[0], "rust": outputs[1], "models": values,
            }
    finally:
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if not all(case["equal"] for case in evidence["cases"].values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
