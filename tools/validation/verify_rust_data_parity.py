"""Compare every structured-data route and its typed output."""

import argparse
import csv
import io
import json
from pathlib import Path
import sys
import tomllib
from xml.etree import ElementTree as ET

import tomli_w
import yaml

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run

ARRAY = {"jsonl", "csv", "tsv"}


def routes(matrix):
    groups = {}
    with matrix.open("rb") as source:
        for line in source:
            row = json.loads(line)
            if row["profile"] != "all" or not row["path"].startswith("input.") or not row["route"]:
                continue
            steps = row["route"]["steps"]
            if len(steps) == 1 and steps[0]["engine_id"] == "data":
                groups.setdefault(row["action"], []).append(steps[0]["source"])
    return groups


def fixture(kind, target, directory):
    path = directory / f"한국어 {kind}.{kind}"
    array = kind in ARRAY or target in ARRAY
    if array:
        model = [{"name": "한글", "content": '=1+1\n둘째 "줄"', "number": 9007199254740993},
                 {"name": "문서", "content": "001\t값", "number": -15}]
    else:
        model = {"name": "한글", "numbers": [9007199254740993, -7, 1.25, 1e-7],
                 "choices": {"true": True, "quoted": "on", "array": ["2025-06-01", 16, "001"]}}
    if kind == "json":
        path.write_text(json.dumps(model, ensure_ascii=False), encoding="utf-8-sig")
    elif kind == "jsonl":
        path.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in model) + "\n", encoding="utf-8")
    elif kind == "yaml":
        path.write_text(yaml.safe_dump(model, allow_unicode=True, sort_keys=False), encoding="utf-8")
    elif kind == "toml":
        path.write_text(tomli_w.dumps(model), encoding="utf-8")
    elif kind == "xml":
        envelope = ET.Element("magic-data", version="1")
        envelope.text = json.dumps(model, ensure_ascii=False)
        path.write_bytes(ET.tostring(envelope, encoding="utf-8", xml_declaration=True))
    else:
        output = io.StringIO(newline="")
        writer = csv.writer(output, delimiter="\t" if kind == "tsv" else ",")
        writer.writerow(model[0].keys())
        writer.writerows(row.values() for row in model)
        path.write_bytes(output.getvalue().encode("cp949"))
    return path


def ordered(value):
    if isinstance(value, dict):
        return ("object", [(key, ordered(item)) for key, item in value.items()])
    if isinstance(value, list):
        return ("array", list(map(ordered, value)))
    return (type(value).__name__, value)


def xml_structure(node):
    return (node.tag, list(node.attrib.items()), node.text, node.tail, list(map(xml_structure, node)))


def decoded(path):
    kind = path.suffix[1:].lower()
    if kind in {"csv", "tsv"}:
        return path.read_bytes()
    text = path.read_text(encoding="utf-8-sig")
    if kind == "json":
        return ordered(json.loads(text))
    if kind == "jsonl":
        return ordered([json.loads(line) for line in text.splitlines() if line.strip()])
    if kind == "yaml":
        return ordered(yaml.safe_load(text))
    if kind == "toml":
        return ordered(tomllib.loads(text))
    root = ET.fromstring(text)
    if root.tag == "magic-data" and root.attrib == {"version": "1"} and len(root) == 0 and root.text:
        return ordered(json.loads(root.text))
    return xml_structure(root)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", required=True, type=Path)
    parser.add_argument("--worker", required=True, type=Path)
    parser.add_argument("--matrix", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    root.mkdir(parents=True)
    worker = args.worker.resolve(strict=True)
    python = [sys.executable, "-B", args.baseline.resolve(strict=True) / "run_magic_converter.py"]
    rust = [worker]
    commands = (("python", python, None), ("rust", rust, native_environment()))
    report = {"status": "running", "worker_sha256": digest(worker), "actions": {}, "extras": {}}

    def save():
        (root / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    def compare(label, files, action, *, success=True):
        outcomes = []
        original = {file: digest(file) for file in files}
        for edition, prefix, env in commands:
            output = root / label / edition
            output.mkdir(parents=True)
            command = ("convert", *files, "--action", action, "--output", output, "--data-dir", root / "profile", "--json")
            code, data = run(prefix, command, environment=env)
            assert code == (0 if success else 1), (action, edition, data)
            job, = data["jobs"]
            assert job["completed"] == (len(files) if success else 0)
            assert not any(output.glob(".magic_converter-*"))
            if not success:
                assert not any(output.iterdir())
            outcomes.append(job)
        assert all(digest(file) == sha for file, sha in original.items())
        left, right = outcomes
        assert left["warnings"] == right["warnings"], (label, left["warnings"], right["warnings"])
        records = []
        for a, b in zip(left["files"], right["files"], strict=True):
            assert (a["state"], a["source"], a["warnings"]) == (b["state"], b["source"], b["warnings"]), (a, b)
            item = {"source": Path(a["source"]).name, "state": a["state"]}
            if success:
                x, = map(Path, a["outputs"])
                y, = map(Path, b["outputs"])
                assert x.name == y.name
                xval, yval = decoded(x), decoded(y)
                assert xval == yval, (label, x, xval, yval)
                item["comparison"] = "bytes" if digest(x) == digest(y) else "typed-value"
            else:
                assert a["errors"] and b["errors"]
                item["errors"] = a["errors"] if a["errors"] == b["errors"] else {"python": a["errors"], "rust": b["errors"]}
            records.append(item)
        return records

    save()
    try:
        for action, sources in routes(args.matrix).items():
            _, target = action.split(":")
            folder = root / "inputs" / target
            folder.mkdir(parents=True)
            fixtures = [fixture(kind, target, folder) for kind in sources]
            report["actions"][action] = compare(target, fixtures, action)
            save()
            print(action, len(sources), flush=True)
        folder = root / "inputs" / "extras"
        folder.mkdir()
        source = folder / "large-int.json"
        source.write_text('{"integer":123456789012345678901234567890, "fraction":1e-7}', encoding="utf-8")
        report["extras"]["yaml-big-integer"] = compare("yaml-big-integer", [source], "data:yaml")
        report["extras"]["toml-oversize-integer"] = compare("toml-oversize-integer", [source], "data:toml", success=False)
        xml = folder / "mixed.xml"
        xml.write_text('<doc xmlns="urn:test" xmlns:x="urn:attrs" x:a="한글">앞<x:item/>뒤<!-- 주석 -->끝</doc>', encoding="utf-8")
        report["extras"]["xml-model"] = compare("xml-model", [xml], "data:json")
        json_path = folder / "model.json"
        json_path.write_text(json.dumps({"$xml": {"tag": "{urn:test}doc", "attributes": {"{urn:attrs}id": "1"},
                         "text": "before", "tail": None, "children": [{"tag": "{urn:test}item", "attributes": {},
                         "text": None, "tail": "after", "children": []}]}}), encoding="utf-8")
        report["extras"]["restore-xml"] = compare("restore-xml", [json_path], "data:xml")
        yml = folder / "typed.yaml"
        yml.write_text("flag: on\nbig: 123456789012345678901234567890\nbase: &base [012, 0xFF, 1:02]\ncopy: *base\n",
                       encoding="utf-8")
        report["extras"]["yaml-1.1-types"] = compare("yaml-1.1-types", [yml], "data:json")
        for name, contents, action in (
            ("duplicate.json", '{"same": 1, "same": 2}', "data:yaml"),
            ("duplicate.yaml", "same: 1\nsame: 2\n", "data:json"),
            ("cycle.yaml", "cycle: &cycle [*cycle]\n", "data:json"),
            ("date.toml", "date = 2026-01-01\n", "data:json"),
            ("date.yaml", "date: 2026-01-01\n", "data:json"),
            ("doctype.xml", "<!DOCTYPE x><x/>", "data:json"),
            ("wrong-columns.csv", "a,b\n1\n", "data:json"),
            ("null.toml.json", '{"x": null}', "data:toml"),
            ("nested-array.json", '[{"x": [1]}]', "data:csv"),
        ):
            source = folder / name
            source.write_text(contents, encoding="utf-8")
            report["extras"][name] = compare(f"rejected-{name}", [source], action, success=False)
        report["status"] = "passed"
    except Exception as error:
        report.update(status="failed", error=f"{type(error).__name__}: {error}")
        raise
    finally:
        save()


if __name__ == "__main__":
    main()
