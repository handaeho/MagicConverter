use roxmltree::{Document, Node, ParsingOptions};
use serde_json::{Map, Value, json};
use std::collections::{HashMap, HashSet};
use std::fs::File;
use std::io::{self, ErrorKind, Read, Write};
use std::path::Path;
use unicode_casefold::UnicodeCaseFold;
use zip::write::SimpleFileOptions;

const KML: &str = "http://www.opengis.net/kml/2.2";
const GPX: &str = "http://www.topografix.com/GPX/1/1";
const DATA_NS: &str = "urn:magic-converter:data:1";
const MAX_FILE: u64 = 64 * 1024 * 1024;
const MAX_PACKAGE: u64 = 256 * 1024 * 1024;
type Check<'a> = &'a dyn Fn() -> io::Result<()>;
type Properties = Map<String, Value>;
type Schemas<'a> = HashMap<&'a str, HashMap<&'a str, &'a str>>;

#[derive(PartialEq)]
struct Position {
    lon: f64,
    lat: f64,
    elevation: Option<f64>,
}

enum Shape {
    Point(Position),
    Line(Vec<Position>),
    Polygon(Vec<Vec<Position>>),
    Points(Vec<Position>),
    Lines(Vec<Vec<Position>>),
    Polygons(Vec<Vec<Vec<Position>>>),
    Collection(Vec<Shape>),
}

struct Feature {
    shape: Shape,
    props: Properties,
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn finite(value: &str) -> io::Result<f64> {
    value
        .trim()
        .parse::<f64>()
        .ok()
        .filter(|v| v.is_finite())
        .ok_or_else(|| invalid("유한한 좌표 또는 숫자가 필요합니다."))
}

fn position(mut values: impl Iterator<Item = io::Result<f64>>) -> io::Result<Position> {
    let missing = || invalid("경위도는 [경도, 위도, 선택 고도]여야 합니다.");
    let lon = values.next().ok_or_else(missing)??;
    let lat = values.next().ok_or_else(missing)??;
    let elevation = values.next().transpose()?;
    if values.next().is_some() {
        return Err(missing());
    }
    if lon.abs() > 180.0 || lat.abs() > 90.0 {
        return Err(invalid(
            "WGS84 경위도 범위를 벗어났습니다. 투영좌표는 먼저 WGS84로 변환하세요.",
        ));
    }
    Ok(Position {
        lon,
        lat,
        elevation,
    })
}

fn json_point(value: &Value) -> io::Result<Position> {
    position(list(value)?.iter().map(|item| {
        if let Some(value) = item.as_str() {
            return finite(value);
        }
        item.as_f64()
            .filter(|v| v.is_finite())
            .ok_or_else(|| invalid("좌표에 숫자가 필요합니다."))
    }))
}

fn list(value: &Value) -> io::Result<&[Value]> {
    value
        .as_array()
        .map(Vec::as_slice)
        .ok_or_else(|| invalid("좌표 목록이 필요합니다."))
}

fn nonempty<T>(values: Vec<T>) -> io::Result<Vec<T>> {
    if values.is_empty() {
        return Err(invalid("비어 있는 좌표 목록입니다."));
    }
    Ok(values)
}

fn line(points: Vec<Position>) -> io::Result<Vec<Position>> {
    if points.len() < 2 {
        return Err(invalid("선에는 두 개 이상의 좌표가 필요합니다."));
    }
    Ok(points)
}

fn polygon(rings: Vec<Vec<Position>>) -> io::Result<Vec<Vec<Position>>> {
    let rings = nonempty(rings)?;
    if rings
        .iter()
        .any(|ring| ring.len() < 4 || ring.first() != ring.last())
    {
        return Err(invalid("다각형 링은 닫혀 있고 네 좌표 이상이어야 합니다."));
    }
    Ok(rings)
}

fn json_points(value: &Value) -> io::Result<Vec<Position>> {
    nonempty(
        list(value)?
            .iter()
            .map(json_point)
            .collect::<io::Result<_>>()?,
    )
}

fn json_polygon(value: &Value) -> io::Result<Vec<Vec<Position>>> {
    polygon(
        list(value)?
            .iter()
            .map(json_points)
            .collect::<io::Result<_>>()?,
    )
}

fn json_shape(value: &Value, depth: usize) -> io::Result<Shape> {
    if depth > 32 {
        return Err(invalid("잘못된 지리 도형입니다."));
    }
    let coords = &value["coordinates"];
    Ok(match value["type"].as_str().unwrap_or_default() {
        "Point" => Shape::Point(json_point(coords)?),
        "LineString" => Shape::Line(line(json_points(coords)?)?),
        "Polygon" => Shape::Polygon(json_polygon(coords)?),
        "MultiPoint" => Shape::Points(json_points(coords)?),
        "MultiLineString" => Shape::Lines(nonempty(
            list(coords)?
                .iter()
                .map(|value| line(json_points(value)?))
                .collect::<io::Result<_>>()?,
        )?),
        "MultiPolygon" => Shape::Polygons(nonempty(
            list(coords)?
                .iter()
                .map(json_polygon)
                .collect::<io::Result<_>>()?,
        )?),
        "GeometryCollection" => Shape::Collection(
            list(&value["geometries"])?
                .iter()
                .map(|item| json_shape(item, depth + 1))
                .collect::<io::Result<_>>()?,
        ),
        _ => return Err(invalid("지원하지 않는 지리 도형입니다.")),
    })
}

fn read_json(
    path: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<Vec<Feature>> {
    let value = crate::data::parse_json(&crate::data::text(&crate::data::bytes(path)?)?, check)?;
    let crs = &value["crs"];
    if !(crs.is_null()
        || crs.as_object().is_some_and(Map::is_empty)
        || matches!(
            crs["properties"]["name"].as_str(),
            Some("EPSG:4326" | "urn:ogc:def:crs:OGC:1.3:CRS84" | "urn:ogc:def:crs:EPSG::4326")
        ))
    {
        return Err(invalid("WGS84 이외의 GeoJSON CRS는 지원하지 않습니다."));
    }
    let items = match value["type"].as_str() {
        Some("FeatureCollection") => list(&value["features"])?,
        Some("Feature") => std::slice::from_ref(&value),
        _ => {
            return Ok(vec![Feature {
                shape: json_shape(&value, 0)?,
                props: Map::new(),
            }]);
        }
    };
    if items.iter().any(|item| {
        item.as_object().is_some_and(|props| {
            props
                .keys()
                .any(|key| !matches!(key.as_str(), "type" | "properties" | "geometry" | "bbox"))
        })
    }) {
        warnings.push("GeoJSON feature ID·bbox·사용자 최상위 필드는 출력하지 않습니다. 속성은 properties에 보관하세요.".to_owned());
    }
    items
        .iter()
        .map(|item| {
            check()?;
            if item["geometry"].is_null() {
                return Err(invalid("위치가 없는 feature는 자동으로 생략하지 않습니다."));
            }
            let props = match &item["properties"] {
                Value::Null => Map::new(),
                Value::Object(props) => props.clone(),
                _ => return Err(invalid("지리 속성은 객체여야 합니다.")),
            };
            Ok(Feature {
                shape: json_shape(&item["geometry"], 0)?,
                props,
            })
        })
        .collect()
}

fn package(path: &Path, check: Check<'_>, warnings: &mut Vec<String>) -> io::Result<Vec<u8>> {
    let mut archive = zip::ZipArchive::new(File::open(path)?)?;
    if archive.len() > 5000 {
        return Err(invalid("지리 패키지 항목 제한을 초과했습니다."));
    }
    let mut bytes = 0_u64;
    let mut names = HashSet::new();
    let mut document = None;
    for i in 0..archive.len() {
        check()?;
        let entry = archive.by_index(i)?;
        bytes = bytes.saturating_add(entry.size());
        if bytes > MAX_PACKAGE {
            return Err(invalid("지리 패키지 크기 제한을 초과했습니다."));
        }
        if entry.is_dir() {
            continue;
        }
        let name = entry.name()?.replace('\\', "/");
        let normalized = name
            .split('/')
            .filter(|part| !part.is_empty())
            .collect::<Vec<_>>()
            .join("/");
        if normalized.is_empty()
            || name.starts_with('/')
            || name.contains(':')
            || name.split('/').any(|part| matches!(part, "." | ".."))
            || entry.size() > MAX_FILE
            || entry
                .unix_mode()
                .is_some_and(|mode| mode & 0o170000 == 0o120000)
            || !names.insert(normalized.case_fold().collect::<String>())
        {
            return Err(invalid("안전하지 않거나 너무 큰 KMZ 항목입니다."));
        }
        if normalized.to_ascii_lowercase().ends_with(".kml") && document.replace(i).is_some() {
            return Err(invalid("KMZ에는 KML 문서가 하나 있어야 합니다."));
        }
    }
    if names.len() > 1 {
        warnings.push("KMZ의 아이콘·오버레이·외부 자산은 보존하지 않습니다.".to_owned());
    }
    let mut entry = archive
        .by_index(document.ok_or_else(|| invalid("KMZ에는 KML 문서가 하나 있어야 합니다."))?)?;
    let mut data = Vec::new();
    (&mut entry).take(MAX_FILE + 1).read_to_end(&mut data)?;
    if data.len() as u64 > MAX_FILE {
        return Err(invalid("KMZ 문서가 너무 큽니다."));
    }
    Ok(data)
}

fn tag<'a>(node: Node<'a, '_>) -> &'a str {
    node.tag_name().name()
}

fn child<'a, 'input: 'a>(
    node: Node<'a, 'input>,
    name: &str,
) -> impl Iterator<Item = Node<'a, 'input>> {
    let name = name.to_owned();
    node.children()
        .filter(move |child| child.is_element() && tag(*child) == name)
}

fn node_text<'a>(node: Node<'a, '_>) -> &'a str {
    node.text().unwrap_or_default()
}

fn kml_points(node: Node<'_, '_>) -> io::Result<Vec<Position>> {
    let values = node
        .descendants()
        .find(|node| tag(*node) == "coordinates")
        .ok_or_else(|| invalid("KML 좌표가 없습니다."))?;
    node_text(values)
        .split_whitespace()
        .map(|values| position(values.split(',').map(finite)))
        .collect()
}

fn kml_shape(node: Node<'_, '_>, depth: usize) -> io::Result<Shape> {
    if depth > 32 {
        return Err(invalid("지리 도형의 중첩이 너무 깊습니다."));
    }
    Ok(match tag(node) {
        "Point" => {
            let mut points = kml_points(node)?;
            if points.len() != 1 {
                return Err(invalid("KML Point 좌표 개수가 잘못되었습니다."));
            }
            Shape::Point(points.pop().unwrap())
        }
        "LineString" => Shape::Line(line(kml_points(node)?)?),
        "Polygon" => {
            let outer: Vec<_> = child(node, "outerBoundaryIs").collect();
            if outer.len() != 1 {
                return Err(invalid("KML 다각형 바깥 링이 하나여야 합니다."));
            }
            let rings = outer
                .into_iter()
                .chain(child(node, "innerBoundaryIs"))
                .map(kml_points)
                .collect::<io::Result<_>>()?;
            Shape::Polygon(polygon(rings)?)
        }
        "MultiGeometry" => Shape::Collection(
            node.children()
                .filter(Node::is_element)
                .map(|node| kml_shape(node, depth + 1))
                .collect::<io::Result<_>>()?,
        ),
        _ => return Err(invalid("지원하지 않는 KML 도형입니다.")),
    })
}

fn schema_fields<'a>(root: Node<'a, '_>) -> io::Result<Schemas<'a>> {
    let mut schemas = HashMap::new();
    for node in root.descendants().filter(|node| {
        tag(*node) == "Schema" && node.tag_name().namespace().is_none_or(|ns| ns == KML)
    }) {
        let id = node
            .attribute("id")
            .filter(|id| !id.is_empty())
            .ok_or_else(|| invalid("KML Schema ID가 없습니다."))?;
        let mut fields = HashMap::new();
        for field in child(node, "SimpleField") {
            match (field.attribute("name"), field.attribute("type")) {
                (Some(name), Some(kind))
                    if !name.is_empty()
                        && !kind.is_empty()
                        && fields.insert(name, kind).is_none() => {}
                _ => {
                    return Err(invalid(
                        "KML SimpleField 이름과 자료형이 필요하며 중복될 수 없습니다.",
                    ));
                }
            }
        }
        if schemas.insert(id, fields).is_some() {
            return Err(invalid("KML Schema ID가 중복됩니다."));
        }
    }
    Ok(schemas)
}

fn scalar(value: &str, kind: &str) -> io::Result<Value> {
    let raw = value.trim();
    let invalid_scalar = || invalid("KML Schema 속성 값이 올바르지 않습니다.");
    macro_rules! number {
        ($kind:ty) => {
            json!(raw.parse::<$kind>().map_err(|_| invalid_scalar())?)
        };
    }
    Ok(match kind {
        "string" => json!(value),
        "short" => number!(i16),
        "ushort" => number!(u16),
        "int" => number!(i32),
        "uint" => number!(u32),
        "float" | "double" => json!(finite(raw)?),
        "bool" if matches!(raw, "1" | "true") => json!(true),
        "bool" if matches!(raw, "0" | "false") => json!(false),
        _ => return Err(invalid_scalar()),
    })
}

fn property(props: &mut Properties, key: Option<&str>, value: Value) -> io::Result<()> {
    if key
        .filter(|key| !key.is_empty())
        .is_none_or(|key| props.insert(key.to_owned(), value).is_some())
    {
        return Err(invalid("KML 속성 이름이 없거나 중복됩니다."));
    }
    Ok(())
}

fn kml_props(
    node: Node<'_, '_>,
    schemas: &Schemas<'_>,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<Properties> {
    let mut props = Map::new();
    for item in node
        .children()
        .filter(|item| matches!(tag(*item), "name" | "description"))
    {
        property(&mut props, Some(tag(item)), json!(node_text(item)))?;
    }
    for extra in child(node, "ExtendedData") {
        for datum in extra.children().filter(Node::is_element) {
            check()?;
            if datum.tag_name().namespace().is_some_and(|ns| ns != KML) {
                return Err(invalid(
                    "지원하지 않는 KML ExtendedData 확장 속성이 있습니다.",
                ));
            }
            match tag(datum) {
                "Data" => {
                    let values: Vec<_> = child(datum, "value").collect();
                    if values.len() != 1 || values[0].children().any(|n| n.is_element()) {
                        return Err(invalid("KML Data에는 단일 텍스트 value가 필요합니다."));
                    }
                    let raw = node_text(values[0]);
                    let value = if datum.attribute((DATA_NS, "encoding")) == Some("json") {
                        crate::data::parse_json(raw, check)?
                    } else {
                        json!(raw)
                    };
                    property(&mut props, datum.attribute("name"), value)?;
                }
                "SchemaData" => {
                    let fields = datum
                        .attribute("schemaUrl")
                        .and_then(|url| url.strip_prefix('#'))
                        .and_then(|id| schemas.get(id));
                    if fields.is_none() {
                        warnings.push("KML Schema가 없거나 외부 참조입니다. 내려받지 않고 SimpleData 값을 문자열로 보존합니다.".to_owned());
                    }
                    for item in datum.children().filter(Node::is_element) {
                        if tag(item) != "SimpleData"
                            || item.tag_name().namespace().is_some_and(|ns| ns != KML)
                            || item.children().any(|n| n.is_element())
                        {
                            return Err(invalid(
                                "지원하지 않는 KML SchemaData 확장 속성이 있습니다.",
                            ));
                        }
                        let key = item.attribute("name");
                        let value = if let Some(fields) = fields {
                            let kind = key.and_then(|key| fields.get(key)).ok_or_else(|| {
                                invalid("KML SimpleData의 Schema 필드가 없습니다.")
                            })?;
                            scalar(node_text(item), kind)?
                        } else {
                            json!(node_text(item))
                        };
                        property(&mut props, key, value)?;
                    }
                }
                _ => {
                    return Err(invalid(
                        "지원하지 않는 KML ExtendedData 확장 속성이 있습니다.",
                    ));
                }
            }
        }
    }
    Ok(props)
}

fn gpx_point(node: Node<'_, '_>) -> io::Result<Position> {
    let coordinates = ["lon", "lat"].into_iter().map(|key| {
        finite(
            node.attribute(key)
                .ok_or_else(|| invalid("GPX 경위도가 없습니다."))?,
        )
    });
    position(coordinates.chain(child(node, "ele").map(|node| finite(node_text(node)))))
}

fn gpx_features(root: Node<'_, '_>, check: Check<'_>) -> io::Result<Vec<Feature>> {
    root.children()
        .filter(|node| matches!(tag(*node), "wpt" | "rte" | "trk"))
        .map(|node| {
            check()?;
            let shape = match tag(node) {
                "wpt" => Shape::Point(gpx_point(node)?),
                "rte" => Shape::Line(line(
                    child(node, "rtept")
                        .map(gpx_point)
                        .collect::<io::Result<_>>()?,
                )?),
                _ => Shape::Lines(nonempty(
                    child(node, "trkseg")
                        .map(|segment| {
                            line(
                                child(segment, "trkpt")
                                    .map(gpx_point)
                                    .collect::<io::Result<_>>()?,
                            )
                        })
                        .collect::<io::Result<_>>()?,
                )?),
            };
            let props = node
                .children()
                .filter_map(|child| {
                    let name = match tag(child) {
                        "name" => "name",
                        "desc" => "description",
                        _ => return None,
                    };
                    Some((name.to_owned(), json!(node_text(child))))
                })
                .collect();
            Ok(Feature { shape, props })
        })
        .collect()
}

fn read(
    path: &Path,
    kind: &str,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<Vec<Feature>> {
    if kind == "geojson" {
        return nonempty(read_json(path, check, warnings)?);
    }
    let bytes = if kind == "kmz" {
        package(path, check, warnings)?
    } else {
        crate::data::bytes(path)?
    };
    let text = crate::data::text(&bytes)?;
    let doc = Document::parse_with_options(
        &text,
        ParsingOptions {
            nodes_limit: 500_000,
            ..Default::default()
        },
    )
    .map_err(io::Error::other)?;
    let root = doc.root_element();
    for (index, node) in root.descendants().enumerate() {
        if index.is_multiple_of(1024) {
            check()?;
        }
        if node.ancestors().take(66).count() >= 66 {
            return Err(invalid("XML 중첩 깊이 제한을 초과했습니다."));
        }
    }
    if kind == "gpx" {
        if tag(root) != "gpx" {
            return Err(invalid("GPX 루트가 없습니다."));
        }
        warnings.push("GPX의 경위도·고도·이름·설명·트랙 구간을 보존합니다. 시각·속도·센서 확장·라우트 구분은 보존하지 않습니다.".to_owned());
        return nonempty(gpx_features(root, check)?);
    }
    if tag(root) != "kml" {
        return Err(invalid("KML 루트가 없습니다."));
    }
    warnings.push("KML 스타일·폴더·시간·고도 모드·네트워크 링크는 보존하지 않습니다. 네트워크 링크를 내려받지 않습니다.".to_owned());
    let schemas = schema_fields(root)?;
    let items = root
        .descendants()
        .filter(|node| tag(*node) == "Placemark")
        .map(|node| {
            check()?;
            let geometries: Vec<_> = node
                .children()
                .filter(|node| {
                    matches!(
                        tag(*node),
                        "Point" | "LineString" | "Polygon" | "MultiGeometry"
                    )
                })
                .collect();
            let [geometry] = geometries.as_slice() else {
                return Err(invalid(
                    "각 KML Placemark에 지원하는 도형 하나가 필요합니다.",
                ));
            };
            Ok(Feature {
                shape: kml_shape(*geometry, 0)?,
                props: kml_props(node, &schemas, check, warnings)?,
            })
        })
        .collect::<io::Result<Vec<_>>>()?;
    nonempty(items)
}

impl Position {
    fn values(&self) -> impl Iterator<Item = f64> {
        [self.lon, self.lat].into_iter().chain(self.elevation)
    }

    fn json(&self) -> Value {
        Value::Array(self.values().map(|value| json!(value)).collect())
    }
}

fn positions_json(points: &[Position]) -> Value {
    Value::Array(points.iter().map(Position::json).collect())
}

fn rings_json(rings: &[Vec<Position>]) -> Value {
    Value::Array(rings.iter().map(|points| positions_json(points)).collect())
}

impl Shape {
    fn json(&self) -> Value {
        let (kind, coordinates) = match self {
            Self::Point(point) => ("Point", point.json()),
            Self::Line(points) => ("LineString", positions_json(points)),
            Self::Polygon(rings) => ("Polygon", rings_json(rings)),
            Self::Points(points) => ("MultiPoint", positions_json(points)),
            Self::Lines(lines) => ("MultiLineString", rings_json(lines)),
            Self::Polygons(polygons) => (
                "MultiPolygon",
                Value::Array(polygons.iter().map(|rings| rings_json(rings)).collect()),
            ),
            Self::Collection(shapes) => {
                return json!({"type": "GeometryCollection", "geometries": shapes.iter().map(Self::json).collect::<Vec<_>>()});
            }
        };
        json!({"type": kind, "coordinates": coordinates})
    }
}

fn xml_text(output: &mut impl Write, tag: &str, value: &str) -> io::Result<()> {
    if value.chars().any(|ch| {
        ch < ' ' && !matches!(ch, '\t' | '\r' | '\n') || matches!(ch, '\u{fffe}' | '\u{ffff}')
    }) {
        return Err(invalid("XML로 기록할 수 없는 문자가 있습니다."));
    }
    write!(output, "<{tag}>{}</{tag}>", html_escape::encode_text(value))
}

fn kml_coordinates(output: &mut impl Write, positions: &[Position]) -> io::Result<()> {
    write!(output, "<coordinates>")?;
    for (index, position) in positions.iter().enumerate() {
        if index > 0 {
            write!(output, " ")?;
        }
        for (index, value) in position.values().enumerate() {
            if index > 0 {
                write!(output, ",")?;
            }
            write!(output, "{value}")?;
        }
    }
    write!(output, "</coordinates>")
}

fn kml_line(output: &mut impl Write, tag: &str, points: &[Position]) -> io::Result<()> {
    write!(output, "<{tag}>")?;
    kml_coordinates(output, points)?;
    write!(output, "</{tag}>")
}

fn kml_polygon(output: &mut impl Write, rings: &[Vec<Position>]) -> io::Result<()> {
    write!(output, "<Polygon>")?;
    for (index, points) in rings.iter().enumerate() {
        let tag = if index == 0 {
            "outerBoundaryIs"
        } else {
            "innerBoundaryIs"
        };
        write!(output, "<{tag}>")?;
        kml_line(output, "LinearRing", points)?;
        write!(output, "</{tag}>")?;
    }
    write!(output, "</Polygon>")
}

fn write_kml_shape(output: &mut impl Write, shape: &Shape) -> io::Result<()> {
    match shape {
        Shape::Point(point) => return kml_line(output, "Point", std::slice::from_ref(point)),
        Shape::Line(points) => return kml_line(output, "LineString", points),
        Shape::Polygon(rings) => return kml_polygon(output, rings),
        _ => {}
    }
    write!(output, "<MultiGeometry>")?;
    match shape {
        Shape::Points(points) => {
            for point in points {
                kml_line(output, "Point", std::slice::from_ref(point))?;
            }
        }
        Shape::Lines(lines) => {
            for points in lines {
                kml_line(output, "LineString", points)?;
            }
        }
        Shape::Polygons(polygons) => {
            for rings in polygons {
                kml_polygon(output, rings)?;
            }
        }
        Shape::Collection(shapes) => {
            for item in shapes {
                write_kml_shape(output, item)?;
            }
        }
        _ => unreachable!(),
    }
    write!(output, "</MultiGeometry>")
}

fn kml(output: &mut impl Write, items: &[Feature], check: Check<'_>) -> io::Result<()> {
    write!(
        output,
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><kml xmlns=\"{KML}\" xmlns:mc=\"{DATA_NS}\"><Document>"
    )?;
    for item in items {
        check()?;
        write!(output, "<Placemark><ExtendedData>")?;
        for (key, value) in &item.props {
            if matches!(key.as_str(), "name" | "description") && value.is_string() {
                continue;
            }
            write!(
                output,
                "<Data name=\"{}\" mc:encoding=\"json\">",
                html_escape::encode_double_quoted_attribute(key)
            )?;
            xml_text(output, "value", &value.to_string())?;
            write!(output, "</Data>")?;
        }
        write!(output, "</ExtendedData>")?;
        for (key, value) in &item.props {
            if matches!(key.as_str(), "name" | "description")
                && let Some(text) = value.as_str()
            {
                xml_text(output, key, text)?;
            }
        }
        write_kml_shape(output, &item.shape)?;
        write!(output, "</Placemark>")?;
    }
    write!(output, "</Document></kml>")
}

fn gpx_metadata(output: &mut impl Write, props: &Properties) -> io::Result<()> {
    for (key, tag) in [("name", "name"), ("description", "desc")] {
        if let Some(value) = props.get(key) {
            let text = match value {
                Value::String(text) => text.clone(),
                Value::Bool(true) => "True".to_owned(),
                Value::Bool(false) => "False".to_owned(),
                Value::Null => "None".to_owned(),
                _ => value.to_string(),
            };
            xml_text(output, tag, &text)?;
        }
    }
    Ok(())
}

fn write_gpx_point(
    output: &mut impl Write,
    tag: &str,
    point: &Position,
    props: Option<&Properties>,
) -> io::Result<()> {
    write!(
        output,
        "<{tag} lat=\"{}\" lon=\"{}\">",
        point.lat, point.lon
    )?;
    if let Some(value) = point.elevation {
        xml_text(output, "ele", &value.to_string())?;
    }
    if let Some(props) = props {
        gpx_metadata(output, props)?;
    }
    write!(output, "</{tag}>")
}

fn gpx(
    output: &mut impl Write,
    items: &[Feature],
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    write!(
        output,
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?><gpx xmlns=\"{GPX}\" version=\"1.1\" creator=\"Magic Converter\">"
    )?;
    for item in items {
        check()?;
        if item
            .props
            .keys()
            .any(|key| !matches!(key.as_str(), "name" | "description"))
        {
            warnings.push("GPX에는 이름·설명 이외의 사용자 속성을 기록하지 않습니다.".to_owned());
        }
        let segments = match &item.shape {
            Shape::Point(point) => {
                write_gpx_point(output, "wpt", point, Some(&item.props))?;
                continue;
            }
            Shape::Line(points) => std::slice::from_ref(points),
            Shape::Lines(lines) => lines.as_slice(),
            _ => {
                return Err(invalid(
                    "GPX는 점·선·트랙 구간만 지원합니다. 다각형/복합 도형은 자동으로 버리지 않습니다.",
                ));
            }
        };
        write!(output, "<trk>")?;
        gpx_metadata(output, &item.props)?;
        for segment in segments {
            write!(output, "<trkseg>")?;
            for point in segment {
                write_gpx_point(output, "trkpt", point, None)?;
            }
            write!(output, "</trkseg>")?;
        }
        write!(output, "</trk>")?;
    }
    write!(output, "</gpx>")
}

pub fn convert(
    source: &Path,
    target: &Path,
    input: &str,
    output: &str,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let items = read(source, input, check, warnings)?;
    let file = File::create(target)?;
    let mut file = io::BufWriter::new(file);
    match output {
        "geojson" => {
            let values: Vec<_> = items.iter().map(|item| json!({"type": "Feature", "properties": item.props, "geometry": item.shape.json()})).collect();
            serde_json::to_writer_pretty(
                &mut file,
                &json!({"type": "FeatureCollection", "features": values}),
            )?;
        }
        "kml" => kml(&mut file, &items, check)?,
        "kmz" => {
            let mut package = zip::ZipWriter::new(&mut file);
            package.start_file(
                "doc.kml",
                SimpleFileOptions::default().compression_method(zip::CompressionMethod::Deflated),
            )?;
            kml(&mut package, &items, check)?;
            package.finish()?;
        }
        "gpx" => gpx(&mut file, &items, check, warnings)?,
        _ => return Err(invalid("지원하지 않는 지리 형식입니다.")),
    }
    file.flush()?;
    drop(file);
    if read(target, output, check, &mut Vec::new())?.len() != items.len() {
        return Err(invalid("출력 지리 feature 수 검증에 실패했습니다."));
    }
    check()
}
