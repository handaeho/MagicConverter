"""WGS84-only GeoJSON/KML/KMZ/GPX; unknown projections are refused."""

import json
import re
import zipfile
from xml.etree import ElementTree as ET
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.specialized.data_converter import unique, validate
from magic_converter.core.specialized.special_io import Package, read_bytes, text, xml, finite
from magic_converter.core.jobs.job_control import check_cancelled

KML = "http://www.opengis.net/kml/2.2"
GPX = "http://www.topografix.com/GPX/1/1"


def local(node):
    return node.tag.rsplit("}", 1)[-1]


def position(value):
    if not isinstance(value, (list, tuple)) or len(value) not in (2, 3):
        raise ConversionError("경위도는 [경도, 위도, 선택 고도]여야 합니다.")
    if any(isinstance(v, bool) for v in value):
        raise ConversionError("논리값을 경위도로 해석하지 않습니다.")
    result = [finite(v) for v in value]
    if abs(result[0]) > 180 or abs(result[1]) > 90:
        raise ConversionError(
            "WGS84 경위도 범위를 벗어났습니다. 투영좌표는 먼저 WGS84로 변환하세요."
        )
    return result


def geometry(value, depth=0):
    if depth > 32 or not isinstance(value, dict):
        raise ConversionError("잘못된 지리 도형입니다.")
    kind = value.get("type")
    if kind == "GeometryCollection":
        return {
            "type": kind,
            "geometries": [geometry(g, depth + 1) for g in value["geometries"]],
        }
    levels = {
        "Point": 0,
        "MultiPoint": 1,
        "LineString": 1,
        "MultiLineString": 2,
        "Polygon": 2,
        "MultiPolygon": 3,
    }
    if kind not in levels:
        raise ConversionError("지원하지 않는 지리 도형입니다.")

    def coords(c, n):
        if n == 0:
            return position(c)
        if not isinstance(c, list) or not c:
            raise ConversionError("비어 있는 좌표 목록입니다.")
        return [coords(x, n - 1) for x in c]

    result = {"type": kind, "coordinates": coords(value["coordinates"], levels[kind])}
    lines = (
        [result["coordinates"]]
        if kind == "LineString"
        else result["coordinates"] if kind == "MultiLineString" else []
    )
    if any(len(line) < 2 for line in lines):
        raise ConversionError("선에는 두 개 이상의 좌표가 필요합니다.")
    polygons = (
        [result["coordinates"]]
        if kind == "Polygon"
        else result["coordinates"] if kind == "MultiPolygon" else []
    )
    if any(
        len(ring) < 4 or ring[0] != ring[-1] for polygon in polygons for ring in polygon
    ):
        raise ConversionError("다각형 링은 닫혀 있고 네 좌표 이상이어야 합니다.")
    return result


def _kml_geometry(node):
    kind = local(node)

    def coordinates(parent):
        e = next((n for n in parent.iter() if local(n) == "coordinates"), None)
        if e is None:
            raise ConversionError("KML 좌표가 없습니다.")
        return [position(v.split(",")) for v in (e.text or "").split()]

    if kind == "Point":
        values = coordinates(node)
        if len(values) != 1:
            raise ConversionError("KML Point 좌표 개수가 잘못되었습니다.")
        return {"type": "Point", "coordinates": values[0]}
    if kind == "LineString":
        return {"type": "LineString", "coordinates": coordinates(node)}
    if kind == "Polygon":
        outer, inner = [], []
        for boundary in node:
            if local(boundary) == "outerBoundaryIs":
                outer.append(coordinates(boundary))
            elif local(boundary) == "innerBoundaryIs":
                inner.append(coordinates(boundary))
        if len(outer) != 1:
            raise ConversionError("KML 다각형 바깥 링이 하나여야 합니다.")
        return {"type": "Polygon", "coordinates": outer + inner}
    if kind == "MultiGeometry":
        return {
            "type": "GeometryCollection",
            "geometries": [_kml_geometry(c) for c in node],
        }
    raise ConversionError(f"지원하지 않는 KML 도형입니다: {kind}")


def _kml_schemas(root):
    schemas = {}
    for node in root.iter():
        if node.tag not in {"Schema", f"{{{KML}}}Schema"}:
            continue
        key = node.get("id")
        if not key or key in schemas:
            raise ConversionError("KML Schema ID가 없거나 중복됩니다.")
        fields = []
        for field in node:
            if local(field) == "SimpleField":
                if not field.get("name") or not field.get("type"):
                    raise ConversionError("KML SimpleField 이름과 자료형이 필요합니다.")
                fields.append((field.get("name"), field.get("type")))
        schemas[key] = unique(fields)
    return schemas


def _kml_scalar(value, kind):
    if kind == "string":
        return value
    stripped = value.strip()
    ranges = {
        "short": (-32768, 32767),
        "ushort": (0, 65535),
        "int": (-(2**31), 2**31 - 1),
        "uint": (0, 2**32 - 1),
    }
    try:
        if kind in ranges:
            if not re.fullmatch(r"[+-]?[0-9]+", stripped):
                raise ValueError()
            number = int(stripped)
            low, high = ranges[kind]
            if not low <= number <= high:
                raise ValueError()
            return number
        if kind == "bool":
            return {"true": True, "false": False, "1": True, "0": False}[stripped]
        if kind in {"float", "double"}:
            return finite(stripped)
    except (ValueError, KeyError) as exc:
        raise ConversionError(
            f"KML {kind} 속성 값이 올바르지 않습니다: {value}"
        ) from exc
    raise ConversionError(f"지원하지 않는 KML Schema 자료형입니다: {kind}")


def _kml_properties(placemark, schemas, notice):
    props = unique(
        (local(n), n.text or "")
        for n in placemark
        if local(n) in {"name", "description"}
    )

    def put(key, value):
        if not key or key in props:
            raise ConversionError("KML 속성 이름이 없거나 중복됩니다.")
        props[key] = value

    for extra in (n for n in placemark if local(n) == "ExtendedData"):
        for node in extra:
            if node.tag in {"Data", f"{{{KML}}}Data"}:
                values = [n for n in node if local(n) == "value"]
                if len(values) != 1 or len(values[0]):
                    raise ConversionError(
                        "KML Data에는 단일 텍스트 value가 필요합니다."
                    )
                value = values[0].text or ""
                if node.get("{urn:magic-converter:data:1}encoding") == "json":
                    value = json.loads(value, object_pairs_hook=unique)
                put(node.get("name"), value)
            elif node.tag in {"SchemaData", f"{{{KML}}}SchemaData"}:
                ref = node.get("schemaUrl", "")
                fields = schemas.get(ref[1:]) if ref.startswith("#") else None
                if fields is None:
                    notice(
                        "KML Schema가 없거나 외부 참조입니다. 내려받지 않고 SimpleData 값을 문자열로 보존합니다."
                    )
                for datum in node:
                    if datum.tag not in {"SimpleData", f"{{{KML}}}SimpleData"}:
                        raise ConversionError(
                            "지원하지 않는 KML SchemaData 확장 속성이 있습니다."
                        )
                    if len(datum):
                        raise ConversionError(
                            "KML SimpleData에는 텍스트 값이 필요합니다."
                        )
                    key, value = datum.get("name"), datum.text or ""
                    if fields is not None:
                        if key not in fields:
                            raise ConversionError(
                                "KML SimpleData의 Schema 필드가 없습니다."
                            )
                        value = _kml_scalar(value, fields[key])
                    put(key, value)
            else:
                raise ConversionError(
                    "지원하지 않는 KML ExtendedData 확장 속성이 있습니다."
                )
    return props


def read_geo(path, kind, notice=lambda _: None):
    features = []
    if kind == "geojson":
        data = json.loads(text(path), object_pairs_hook=unique)
        validate(data)
        if data.get("crs"):
            name = data["crs"].get("properties", {}).get("name", "")
            if name not in {
                "EPSG:4326",
                "urn:ogc:def:crs:OGC:1.3:CRS84",
                "urn:ogc:def:crs:EPSG::4326",
            }:
                raise ConversionError("WGS84 이외의 GeoJSON CRS는 지원하지 않습니다.")
        if data.get("type") == "FeatureCollection":
            features = data["features"]
        elif data.get("type") == "Feature":
            features = [data]
        else:
            features = [{"type": "Feature", "properties": {}, "geometry": data}]
        if any(
            "id" in f or set(f) - {"type", "properties", "geometry", "bbox"}
            for f in features
        ):
            notice(
                "GeoJSON feature ID·bbox·사용자 최상위 필드는 출력하지 않습니다. 속성은 properties에 보관하세요."
            )
    else:
        if kind == "kmz":
            with Package(path) as package:
                names = [n for n in package.names if n.lower().endswith(".kml")]
                if len(names) != 1:
                    raise ConversionError("KMZ에는 KML 문서가 하나 있어야 합니다.")
                root = xml(package.read(names[0]))
                if len(package.names) > 1:
                    notice("KMZ의 아이콘·오버레이·외부 자산은 보존하지 않습니다.")
        else:
            root = xml(read_bytes(path))
        if kind in {"kml", "kmz"}:
            if local(root) != "kml":
                raise ConversionError("KML 루트가 없습니다.")
            notice(
                "KML 스타일·폴더·시간·고도 모드·네트워크 링크는 보존하지 않습니다. 네트워크 링크를 내려받지 않습니다."
            )
            schemas = _kml_schemas(root)
            for placemark in (n for n in root.iter() if local(n) == "Placemark"):
                check_cancelled()
                shapes = [
                    n
                    for n in placemark
                    if local(n) in {"Point", "LineString", "Polygon", "MultiGeometry"}
                ]
                if len(shapes) != 1:
                    raise ConversionError(
                        "각 KML Placemark에 지원하는 도형 하나가 필요합니다."
                    )
                props = _kml_properties(placemark, schemas, notice)
                features.append(
                    {"properties": props, "geometry": _kml_geometry(shapes[0])}
                )
        elif kind == "gpx":
            if local(root) != "gpx":
                raise ConversionError("GPX 루트가 없습니다.")
            notice(
                "GPX의 경위도·고도·이름·설명·트랙 구간을 보존합니다. 시각·속도·센서 확장·라우트 구분은 보존하지 않습니다."
            )

            def point(node):
                values = [finite(node.attrib["lon"]), finite(node.attrib["lat"])]
                values.extend(finite(n.text) for n in node if local(n) == "ele")
                return position(values)

            for node in root:
                tag = local(node)
                if tag not in {"wpt", "rte", "trk"}:
                    continue
                props = {
                    local(n): n.text or "" for n in node if local(n) in {"name", "desc"}
                }
                if "desc" in props:
                    props["description"] = props.pop("desc")
                if tag == "wpt":
                    shape = {"type": "Point", "coordinates": point(node)}
                elif tag == "rte":
                    shape = {
                        "type": "LineString",
                        "coordinates": [point(n) for n in node if local(n) == "rtept"],
                    }
                else:
                    segments = [
                        [point(n) for n in seg if local(n) == "trkpt"]
                        for seg in node
                        if local(seg) == "trkseg"
                    ]
                    shape = {"type": "MultiLineString", "coordinates": segments}
                features.append({"properties": props, "geometry": shape})
        else:
            raise ConversionError("지원하지 않는 지리 형식입니다.")
    result = []
    if not features:
        raise ConversionError("변환할 지리 feature가 없습니다.")
    for feature in features:
        check_cancelled()
        if feature.get("geometry") is None:
            raise ConversionError("위치가 없는 feature는 자동으로 생략하지 않습니다.")
        props = feature.get("properties") or {}
        if not isinstance(props, dict):
            raise ConversionError("지리 속성은 객체여야 합니다.")
        validate(props)
        result.append(
            {
                "type": "Feature",
                "properties": props,
                "geometry": geometry(feature["geometry"]),
            }
        )
    return result


def write_geo(features, path, kind, notice=lambda _: None):
    if kind == "geojson":
        path.write_text(
            json.dumps(
                {"type": "FeatureCollection", "features": features},
                ensure_ascii=False,
                indent=2,
                allow_nan=False,
            ),
            encoding="utf-8",
        )
        return
    ns = KML if kind in {"kml", "kmz"} else GPX

    def sub(parent, tag, body=None, **attrs):
        node = ET.SubElement(parent, f"{{{ns}}}{tag}", attrs)
        node.text = body
        return node

    root = ET.Element(
        f"{{{ns}}}" + ("kml" if ns == KML else "gpx"),
        {} if ns == KML else {"version": "1.1", "creator": "Magic Converter"},
    )
    parent = sub(root, "Document") if ns == KML else root

    def kshape(parent, g):
        t = g["type"]
        if t in {"GeometryCollection", "MultiPoint", "MultiLineString", "MultiPolygon"}:
            node = sub(parent, "MultiGeometry")
            children = (
                g["geometries"]
                if t == "GeometryCollection"
                else [{"type": t[5:], "coordinates": c} for c in g["coordinates"]]
            )
            for child in children:
                kshape(node, child)
        elif t in {"Point", "LineString"}:
            node = sub(parent, t)
            coords = [g["coordinates"]] if t == "Point" else g["coordinates"]
            sub(
                node,
                "coordinates",
                " ".join(",".join(format(v, ".17g") for v in p) for p in coords),
            )
        elif t == "Polygon":
            node = sub(parent, t)
            for i, ring in enumerate(g["coordinates"]):
                boundary = sub(node, "outerBoundaryIs" if i == 0 else "innerBoundaryIs")
                sub(
                    sub(boundary, "LinearRing"),
                    "coordinates",
                    " ".join(",".join(format(v, ".17g") for v in p) for p in ring),
                )

    def gpoint(parent, tag, p):
        node = sub(parent, tag, lat=format(p[1], ".17g"), lon=format(p[0], ".17g"))
        if len(p) == 3:
            sub(node, "ele", str(p[2]))
        return node

    for feature in features:
        check_cancelled()
        g, props = feature["geometry"], feature["properties"]
        if ns == KML:
            placemark = sub(parent, "Placemark")
            extra = sub(placemark, "ExtendedData")
            for key, value in props.items():
                if key in {"name", "description"} and isinstance(value, str):
                    sub(placemark, key, value)
                else:
                    datum = sub(extra, "Data", name=key)
                    datum.set("{urn:magic-converter:data:1}encoding", "json")
                    sub(
                        datum,
                        "value",
                        json.dumps(value, ensure_ascii=False, allow_nan=False),
                    )
            kshape(placemark, g)
        else:
            if g["type"] == "Point":
                node = gpoint(parent, "wpt", g["coordinates"])
            elif g["type"] in {"LineString", "MultiLineString"}:
                node = sub(parent, "trk")
                for segment in (
                    [g["coordinates"]]
                    if g["type"] == "LineString"
                    else g["coordinates"]
                ):
                    seg = sub(node, "trkseg")
                    for p in segment:
                        gpoint(seg, "trkpt", p)
            else:
                raise ConversionError(
                    "GPX는 점·선·트랙 구간만 지원합니다. 다각형/복합 도형은 자동으로 버리지 않습니다."
                )
            children = []
            for key, tag in (("name", "name"), ("description", "desc")):
                if key in props:
                    # GPX metadata precedes segments/ele in its schema.
                    child = ET.Element(f"{{{ns}}}{tag}")
                    child.text = str(props[key])
                    children.append(child)
            offset = 1 if len(node) and local(node[0]) == "ele" else 0
            for index, child in enumerate(children):
                node.insert(offset + index, child)
            if set(props) - {"name", "description"}:
                notice("GPX에는 이름·설명 이외의 사용자 속성을 기록하지 않습니다.")
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    xml(data)
    if kind == "kmz":
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as archive:
            archive.writestr("doc.kml", data)
    else:
        path.write_bytes(data)


def convert(source, destination, source_kind, target_kind, notice):
    features = read_geo(source, source_kind, notice)
    write_geo(features, destination, target_kind, notice)
    result = read_geo(destination, target_kind)
    if len(result) != len(features):
        raise ConversionError("출력 지리 feature 수 검증에 실패했습니다.")
