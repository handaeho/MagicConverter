"""Check all geography routes and supported workbook conversions on shared fixtures."""

import argparse
from collections import Counter
from datetime import datetime
import json
from pathlib import Path
import sys
from time import perf_counter
from zipfile import ZipFile

from openpyxl import Workbook, load_workbook

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
from tools.validation.verify_rust_full_parity import digest, native_environment, run

KML = "http://www.opengis.net/kml/2.2"
GPX = "http://www.topografix.com/GPX/1/1"
OFFICE = "urn:oasis:names:tc:opendocument:xmlns:office:1.0"
TABLE = "urn:oasis:names:tc:opendocument:xmlns:table:1.0"
TEXT = "urn:oasis:names:tc:opendocument:xmlns:text:1.0"
FORMULA = "urn:oasis:names:tc:opendocument:xmlns:of:1.2"


def geo_fixtures(folder):
    props = {"name": "한글 & text", "description": "coordinate", "value": 3, "active": True}
    geojson = folder / "map.geojson"
    geojson.write_text(json.dumps({"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": {"type": "Point", "coordinates": [127.01, 37.02, 0.2]}, "properties": props},
        {"type": "Feature", "geometry": {"type": "LineString", "coordinates": [[127.2, 37.2], [127.3, 37.3]]}, "properties": {}},
    ]}, ensure_ascii=False), encoding="utf-8")
    kml_data = (f'<kml xmlns="{KML}"><Document><Schema id="s"><SimpleField name="value" type="int"/>'
                '<SimpleField name="active" type="bool"/></Schema><Placemark><name>한글 &amp; text</name>'
                '<ExtendedData><SchemaData schemaUrl="#s"><SimpleData name="value">3</SimpleData>'
                '<SimpleData name="active">true</SimpleData></SchemaData></ExtendedData>'
                '<Point><coordinates>127.01,37.02,0.2</coordinates></Point></Placemark>'
                '<Placemark><LineString><coordinates>127.2,37.2 127.3,37.3</coordinates></LineString>'
                '</Placemark></Document></kml>')
    kml = folder / "map.kml"
    kml.write_text(kml_data, encoding="utf-8")
    kmz = folder / "map.kmz"
    with ZipFile(kmz, "w") as package:
        package.writestr("assets/icon.png", b"asset placeholder")
        package.writestr("geo/Map.KML", kml_data)
    gpx = folder / "map.gpx"
    gpx.write_text(f'<gpx xmlns="{GPX}"><wpt lat="37.02" lon="127.01"><ele>0.2</ele><name>한글</name></wpt>'
                   '<trk><desc>segments</desc><trkseg><trkpt lon="127.2" lat="37.2"/><trkpt lon="127.3" lat="37.3"/></trkseg>'
                   '<trkseg><trkpt lon="128.2" lat="38.2"/><trkpt lon="128.3" lat="38.3"/></trkseg></trk></gpx>', encoding="utf-8")
    polygon = folder / "polygon.geojson"
    polygon.write_text('{"type":"Polygon","coordinates":[[[0,0],[1,0],[1,1],[0,0]]]}', encoding="utf-8")
    duplicate = folder / "duplicate.geojson"
    duplicate.write_text('{"type":"Point","coordinates":[0,0],"coordinates":[1,1]}', encoding="utf-8")
    invalid = folder / "invalid.gpx"
    invalid.write_text('<gpx><wpt lat="91" lon="120"/></gpx>', encoding="utf-8")
    return (geojson, kml, kmz, gpx), (polygon, duplicate, invalid)


def workbook_fixtures(folder):
    book = Workbook()
    data = book.active
    data.title = "매출 & 2026"
    data.append(["정수", "날짜", "수식", "문자열", "논리"])
    data.append([1, datetime(2026, 3, 5, 10, 20), "=A2*2", "=literal", True])
    data["D2"].data_type = "s"
    data.append([None, None, "=SUM(A2:A2)", "line\nquote\""])
    other = book.create_sheet("원본 값")
    other["C3"] = "keep"
    xlsx = folder / "sample.xlsx"
    book.save(xlsx)
    variants = []
    for extension, main_type in (
        ("xlsm", "application/vnd.ms-excel.sheet.macroEnabled.main+xml"),
        ("xltm", "application/vnd.ms-excel.template.macroEnabled.main+xml"),
        ("xltx", "application/vnd.openxmlformats-officedocument.spreadsheetml.template.main+xml"),
    ):
        variant = folder / f"sample.{extension}"
        with ZipFile(xlsx) as original, ZipFile(variant, "w") as transformed:
            for name in original.namelist():
                data = original.read(name)
                if name == "[Content_Types].xml":
                    data = data.replace(b"application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml", main_type.encode())
                transformed.writestr(name, data)
            if extension.endswith("m"):
                transformed.writestr("xl/vbaProject.bin", b"synthetic payload")
        variants.append(variant)
    ods = folder / "sample.ods"
    content = (
        f'<office:document-content xmlns:office="{OFFICE}" xmlns:table="{TABLE}" xmlns:text="{TEXT}" xmlns:of="{FORMULA}">'
        '<office:body><office:spreadsheet><table:table table:name="자료"><table:table-row>'
        '<table:table-cell office:value-type="float" office:value="2.0"/>'
        '<table:table-cell table:number-columns-repeated="2"/>'
        '<table:table-cell table:formula="of:=[.A1]*2" office:value-type="float" office:value="4"/>'
        '<table:table-cell office:value-type="boolean" office:boolean-value="true"/>'
        '</table:table-row><table:table-row table:number-rows-repeated="2">'
        '<table:table-cell table:formula="of:=[.A1]+1" office:value-type="float" office:value="3"/>'
        '<table:table-cell office:value-type="string"><text:p>한글</text:p><text:p>값</text:p></table:table-cell>'
        '</table:table-row></table:table><table:table table:name="빈 시트"/></office:spreadsheet></office:body></office:document-content>'
    )
    with ZipFile(ods, "w") as package:
        package.writestr("mimetype", "application/vnd.oasis.opendocument.spreadsheet")
        package.writestr("content.xml", content)
    csv = folder / "한국어.csv"
    csv.write_bytes('\r\n"문자,열",=literal\r\n\r\n"줄\n바꿈","""따옴표"""\n""\n,\n'.encode("utf-8-sig"))
    tsv = folder / "한글.tsv"
    tsv.write_bytes('값\t"탭\t셀"\r\n"새\r\n줄"\t문자열\n\n마지막\t\r\n'.encode("utf-16"))
    return xlsx, ods, csv, tsv, *variants


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--worker", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--xls", type=Path)
    args = parser.parse_args()
    root = args.output.resolve()
    inputs = root / "inputs"
    inputs.mkdir(parents=True)
    baseline = args.baseline.resolve()
    sys.path.insert(0, str(baseline))
    from magic_converter.core.documents.spreadsheet_converter import read_sheets
    from magic_converter.core.specialized.geo_converter import read_geo

    def spreadsheet(path):
        if path.is_dir():
            return {file.name: file.read_bytes().hex() for file in sorted(path.iterdir())}
        if path.suffix in (".csv", ".tsv"):
            return path.read_bytes().hex()
        if path.suffix == ".ods":
            return read_sheets(path)
        with path.open("rb") as stream:
            book = load_workbook(stream, data_only=False)
            return {sheet.title: [[(c.value, c.data_type) for c in row] for row in sheet.iter_rows()]
                    for sheet in book}

    def geo(path):
        return read_geo(path, path.suffix[1:])

    routes = []
    maps, errors = geo_fixtures(inputs)
    for item in maps:
        for fmt in ("geojson", "kml", "kmz", "gpx"):
            if item.suffix[1:] != fmt:
                routes.append((item, f"geo:{fmt}", geo, 0))
    routes += [(errors[0], "geo:gpx", geo, 1), (errors[1], "geo:kml", geo, 1), (errors[2], "geo:geojson", geo, 1)]
    tables = workbook_fixtures(inputs)
    if args.xls:
        tables += (args.xls.resolve(),)
    for item in tables:
        for fmt in ("csv", "tsv", "xlsx", "ods"):
            routes.append((item, f"sheet:{fmt}", spreadsheet, 0))
        if item.suffix in (".csv", ".tsv"):
            fmt = "tsv" if item.suffix == ".csv" else "csv"
            routes.append((item, f"data:{fmt}", spreadsheet, 0))

    evidence = {"worker_sha256": digest(args.worker), "cases": {}}
    try:
        for item, action, model, expected in routes:
            key = f"{item.name}-{action.replace(':', '-')}"
            records = {}
            values = []
            for name, prefix, environment in (
                ("python", [sys.executable, "-B", baseline / "run_magic_converter.py"], None),
                ("rust", [args.worker.resolve()], native_environment()),
            ):
                destination = root / key / name
                destination.mkdir(parents=True)
                start = perf_counter()
                code, report = run(prefix, ("convert", item, "--action", action, "--output", destination,
                                            "--data-dir", root / f"profile-{name}", "--json"), environment=environment)
                records[name] = {"code": code, "s": perf_counter() - start, "report": report}
                values.append({p.name: model(p) for p in destination.iterdir()} if code == 0 else list(destination.iterdir()))
            records["equal"] = all(records[name]["code"] == expected for name in ("python", "rust")) and values[0] == values[1]
            if not records["equal"]:
                records["models"] = values
            evidence["cases"][key] = records
    finally:
        evidence["counts"] = Counter("equal" if case["equal"] else "different" for case in evidence["cases"].values())
        (root / "report.json").write_text(json.dumps(evidence, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    if evidence["counts"].get("different"):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
