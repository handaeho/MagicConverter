//! Geometry-only OBJ, STL and PLY interchange.
use std::collections::{BTreeSet, HashSet};
use std::fs::File;
use std::io::{self, BufRead, Cursor, ErrorKind, Read, Write};
use std::path::Path;

type Check<'a> = &'a dyn Fn() -> io::Result<()>;
type Point = [f64; 3];
type Face = [usize; 3];
const LIMIT: usize = 1_000_000;
const MESH_LIMIT: &str = "비어 있지 않은 삼각형 메시가 필요합니다 (정점/면 각 100만 개까지).";

#[derive(Default)]
struct Mesh {
    vertices: Vec<Point>,
    faces: Vec<Face>,
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

fn finite(value: &str) -> io::Result<f64> {
    let value: f64 = value.parse().map_err(io::Error::other)?;
    if !value.is_finite() {
        return Err(invalid("무한대/NaN 좌표는 변환할 수 없습니다."));
    }
    Ok(value)
}

fn point(values: &[&str]) -> io::Result<Point> {
    let [x, y, z] = values else {
        return Err(invalid("잘못된 메시 좌표입니다."));
    };
    Ok([finite(x)?, finite(y)?, finite(z)?])
}

impl Mesh {
    fn bound(&self) -> io::Result<()> {
        if self.vertices.len() > LIMIT || self.faces.len() > LIMIT {
            Err(invalid(MESH_LIMIT))
        } else {
            Ok(())
        }
    }

    fn validate(&self, check: Check<'_>) -> io::Result<()> {
        self.bound()?;
        if self.vertices.is_empty() || self.faces.is_empty() {
            return Err(invalid(MESH_LIMIT));
        }
        for batch in self.vertices.chunks(1024) {
            check()?;
            if batch.iter().flatten().any(|value| !value.is_finite()) {
                return Err(invalid("잘못된 메시 좌표입니다."));
            }
        }
        for batch in self.faces.chunks(1024) {
            check()?;
            for &[a, b, c] in batch {
                if [a, b, c].into_iter().any(|i| i >= self.vertices.len()) {
                    return Err(invalid("잘못된 메시 면 인덱스입니다."));
                }
                if a == b || b == c || a == c {
                    return Err(invalid("중복 정점으로 이루어진 면입니다."));
                }
            }
        }
        Ok(())
    }

    fn triangle(&mut self, points: [Point; 3]) -> io::Result<()> {
        let n = self.vertices.len();
        self.vertices.extend(points);
        self.faces.push([n, n + 1, n + 2]);
        self.bound()
    }
}

fn cross([ax, ay]: [f64; 2], [bx, by]: [f64; 2], [cx, cy]: [f64; 2]) -> f64 {
    (bx - ax) * (cy - ay) - (by - ay) * (cx - ax)
}

fn triangulate(indices: &[usize], mesh: &mut Mesh, check: Check<'_>) -> io::Result<()> {
    if !(3..=10000).contains(&indices.len())
        || indices.iter().any(|&i| i >= mesh.vertices.len())
        || indices.iter().collect::<HashSet<_>>().len() != indices.len()
    {
        return Err(invalid("잘못된 다각형 인덱스입니다."));
    }
    if let [a, b, c] = indices {
        mesh.faces.push([*a, *b, *c]);
        return mesh.bound();
    }
    let origin = mesh.vertices[indices[0]];
    let points: Vec<Point> = indices
        .iter()
        .map(|&i| std::array::from_fn(|j| mesh.vertices[i][j] - origin[j]))
        .collect();
    let pairs = || points.iter().zip(points.iter().cycle().skip(1));
    let normal: Point = std::array::from_fn(|j| {
        pairs()
            .map(|(p, q)| (p[(j + 1) % 3] - q[(j + 1) % 3]) * (p[(j + 2) % 3] + q[(j + 2) % 3]))
            .sum()
    });
    let length = normal.iter().map(|x| x * x).sum::<f64>().sqrt();
    if !length.is_finite() || length < 1e-15 {
        return Err(invalid("퇴화하거나 교차하는 다각형입니다."));
    }
    let scale = points
        .iter()
        .flatten()
        .fold(1.0_f64, |scale, x| scale.max(x.abs()));
    if points.iter().any(|p| {
        p.iter().zip(normal).map(|(x, n)| x * n).sum::<f64>().abs() > length * scale * 1e-7
    }) {
        return Err(invalid("비평면 다각형은 먼저 삼각형으로 분할하세요."));
    }
    let mut axis = 0;
    for i in 1..3 {
        if normal[i].abs() > normal[axis].abs() {
            axis = i;
        }
    }
    let points: Vec<_> = points
        .into_iter()
        .map(|[x, y, z]| match axis {
            0 => [y, z],
            1 => [x, z],
            _ => [x, y],
        })
        .collect();
    let n = points.len();
    for i in 0..n {
        check()?;
        for j in i + 2..n {
            if i == 0 && j == n - 1 {
                continue;
            }
            let (a, b, c, d) = (
                points[i],
                points[(i + 1) % n],
                points[j],
                points[(j + 1) % n],
            );
            if cross(a, b, c) * cross(a, b, d) < 0.0 && cross(c, d, a) * cross(c, d, b) < 0.0 {
                return Err(invalid("교차하는 다각형은 변환할 수 없습니다."));
            }
        }
    }
    let area: f64 = points
        .iter()
        .zip(points.iter().cycle().skip(1))
        .map(|([x, y], [u, v])| x * v - u * y)
        .sum();
    let sign = if area > 0.0 { 1.0 } else { -1.0 };
    let mut pending: Vec<_> = (0..n).collect();
    while pending.len() > 3 {
        check()?;
        let len = pending.len();
        let cut = pending
            .iter()
            .enumerate()
            .find_map(|(i, &b)| {
                let (a, c) = (pending[(i + len - 1) % len], pending[(i + 1) % len]);
                let edges = [(a, b), (b, c), (c, a)];
                let occupied = || {
                    pending.iter().any(|p| {
                        ![a, b, c].contains(p)
                            && edges.iter().all(|&(x, y)| {
                                sign * cross(points[x], points[y], points[*p]) >= -1e-14
                            })
                    })
                };
                (sign * cross(points[a], points[b], points[c]) > 1e-14 && !occupied())
                    .then_some((i, [a, b, c]))
            })
            .ok_or_else(|| invalid("다각형 삼각분할에 실패했습니다. 입력 메시를 확인하세요."))?;
        mesh.faces.push(cut.1.map(|index| indices[index]));
        mesh.bound()?;
        pending.remove(cut.0);
    }
    mesh.faces.push([
        indices[pending[0]],
        indices[pending[1]],
        indices[pending[2]],
    ]);
    mesh.bound()
}

fn obj(
    input: &[u8],
    mesh: &mut Mesh,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let input = std::str::from_utf8(input.strip_prefix(b"\xef\xbb\xbf").unwrap_or(input))
        .map_err(io::Error::other)?;
    let mut omitted = BTreeSet::new();
    for (i, line) in input.lines().enumerate() {
        if i % 1024 == 0 {
            check()?;
        }
        let row = line
            .split('#')
            .next()
            .unwrap_or("")
            .split_whitespace()
            .collect::<Vec<_>>();
        let Some((tag, values)) = row.split_first() else {
            continue;
        };
        match *tag {
            "v" => {
                if values.len() != 3 {
                    return Err(invalid(
                        "OBJ는 XYZ 정점만 지원합니다. 정점 색상·동차 좌표는 먼저 정규화하세요.",
                    ));
                }
                mesh.vertices.push(point(values)?);
            }
            "f" => {
                let mut indices = Vec::new();
                for value in values {
                    let (index, attributes) = value
                        .split_once('/')
                        .map_or((*value, false), |(index, _)| (index, true));
                    if attributes {
                        omitted.insert("UV/법선");
                    }
                    let index: i64 = index.parse().map_err(io::Error::other)?;
                    if index == 0 {
                        return Err(invalid("OBJ 인덱스 0은 유효하지 않습니다."));
                    }
                    indices.push(
                        usize::try_from(if index > 0 {
                            index - 1
                        } else {
                            mesh.vertices.len() as i64 + index
                        })
                        .map_err(|_| invalid("잘못된 다각형 인덱스입니다."))?,
                    );
                }
                triangulate(&indices, mesh, check)?;
            }
            "o" | "g" | "s" => (),
            value => {
                omitted.insert(value);
            }
        }
        mesh.bound()?;
    }
    if !omitted.is_empty() {
        warnings.push(format!(
            "OBJ의 재질·텍스처·법선·선/점 개체는 출력하지 않습니다: {}",
            omitted.into_iter().collect::<Vec<_>>().join(", ")
        ));
    }
    Ok(())
}

fn stl(input: &[u8], mesh: &mut Mesh, check: Check<'_>) -> io::Result<()> {
    if let Some(count) = input
        .get(80..84)
        .map(|count| u32::from_le_bytes(count.try_into().unwrap()))
        && input.len() as u64 == 84 + 50 * u64::from(count)
    {
        for (index, bytes) in input[84..].chunks_exact(50).enumerate() {
            if index % 1024 == 0 {
                check()?;
            }
            mesh.triangle(std::array::from_fn(|point| {
                std::array::from_fn(|component| {
                    let start = 12 + point * 12 + component * 4;
                    f64::from(f32::from_le_bytes(
                        bytes[start..start + 4].try_into().unwrap(),
                    ))
                })
            }))?;
        }
        return Ok(());
    }
    if !input.is_ascii() {
        return Err(invalid("올바른 ASCII/바이너리 STL이 아닙니다."));
    }
    let input = std::str::from_utf8(input).map_err(io::Error::other)?;
    let mut rows = input.lines().map(str::trim).filter(|line| !line.is_empty());
    if !rows
        .next()
        .and_then(|row| row.split_whitespace().next())
        .is_some_and(|tag| tag.eq_ignore_ascii_case("solid"))
    {
        return Err(invalid("올바른 ASCII/바이너리 STL이 아닙니다."));
    }
    loop {
        check()?;
        let bad = || invalid("STL 면 구조가 잘못되었습니다.");
        let row = rows
            .next()
            .ok_or_else(bad)?
            .split_whitespace()
            .collect::<Vec<_>>();
        if row[0].eq_ignore_ascii_case("endsolid") && rows.next().is_none() {
            break;
        }
        if !row.starts_with(&["facet", "normal"])
            || rows
                .next()
                .map(|line| line.split_whitespace().collect::<Vec<_>>())
                != Some(vec!["outer", "loop"])
        {
            return Err(bad());
        }
        let mut points = [[0.0; 3]; 3];
        for vertex in &mut points {
            let row = rows
                .next()
                .ok_or_else(bad)?
                .split_whitespace()
                .collect::<Vec<_>>();
            if row.first() != Some(&"vertex") {
                return Err(bad());
            }
            *vertex = point(&row[1..])?;
        }
        if rows.next() != Some("endloop") || rows.next() != Some("endfacet") {
            return Err(bad());
        }
        mesh.triangle(points)?;
    }
    Ok(())
}

#[derive(Clone, Copy)]
enum Scalar {
    I8,
    U8,
    I16,
    U16,
    I32,
    U32,
    F32,
    F64,
}
impl Scalar {
    fn parse(kind: &str) -> io::Result<Self> {
        Ok(match kind {
            "char" | "int8" => Self::I8,
            "uchar" | "uint8" => Self::U8,
            "short" | "int16" => Self::I16,
            "ushort" | "uint16" => Self::U16,
            "int" | "int32" => Self::I32,
            "uint" | "uint32" => Self::U32,
            "float" | "float32" => Self::F32,
            "double" | "float64" => Self::F64,
            _ => return Err(invalid("지원하지 않는 PLY 속성 자료형입니다.")),
        })
    }
    fn integer(self) -> bool {
        !matches!(self, Self::F32 | Self::F64)
    }
}

enum PlyInput<'a> {
    Ascii(std::str::SplitWhitespace<'a>),
    Binary(Cursor<&'a [u8]>, bool),
}

impl PlyInput<'_> {
    fn scalar(&mut self, ty: Scalar) -> io::Result<f64> {
        Ok(match self {
            Self::Ascii(tokens) => {
                let value = tokens
                    .next()
                    .ok_or_else(|| invalid("PLY 항목이 중단되었습니다."))?;
                if ty.integer() {
                    value.parse::<i64>().map_err(io::Error::other)? as f64
                } else {
                    finite(value)?
                }
            }
            Self::Binary(stream, little) => {
                fn bytes<const N: usize>(
                    stream: &mut impl Read,
                    little: bool,
                ) -> io::Result<[u8; N]> {
                    let mut data = [0; N];
                    stream.read_exact(&mut data)?;
                    if !little {
                        data.reverse();
                    }
                    Ok(data)
                }
                match ty {
                    Scalar::I8 => f64::from(i8::from_le_bytes(bytes(stream, *little)?)),
                    Scalar::U8 => f64::from(u8::from_le_bytes(bytes(stream, *little)?)),
                    Scalar::I16 => f64::from(i16::from_le_bytes(bytes(stream, *little)?)),
                    Scalar::U16 => f64::from(u16::from_le_bytes(bytes(stream, *little)?)),
                    Scalar::I32 => f64::from(i32::from_le_bytes(bytes(stream, *little)?)),
                    Scalar::U32 => f64::from(u32::from_le_bytes(bytes(stream, *little)?)),
                    Scalar::F32 => f64::from(f32::from_le_bytes(bytes(stream, *little)?)),
                    Scalar::F64 => f64::from_le_bytes(bytes(stream, *little)?),
                }
            }
        })
    }
}

struct Property {
    name: String,
    ty: Scalar,
    list: Option<Scalar>,
}
struct Element {
    name: String,
    count: usize,
    properties: Vec<Property>,
}

fn ply(
    data: &[u8],
    mesh: &mut Mesh,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut reader = Cursor::new(data);
    let mut row = String::new();
    reader.read_line(&mut row)?;
    if row.trim() != "ply" {
        return Err(invalid("PLY 헤더가 없습니다."));
    }
    let mut encoding = None;
    let mut elements: Vec<Element> = Vec::new();
    let mut finished = false;
    for _ in 0..10000 {
        check()?;
        row.clear();
        reader.read_line(&mut row)?;
        let words = row.split_whitespace().collect::<Vec<_>>();
        match words.as_slice() {
            [] => return Err(invalid("PLY 헤더가 중단되었습니다.")),
            ["end_header"] => {
                finished = true;
                break;
            }
            ["format", kind, _] => encoding = Some((*kind).to_owned()),
            ["element", name, count] => {
                let count: usize = count
                    .parse()
                    .map_err(|_| invalid("PLY 항목 개수 제한을 초과했습니다."))?;
                if count > LIMIT {
                    return Err(invalid("PLY 항목 개수 제한을 초과했습니다."));
                }
                elements.push(Element {
                    name: (*name).to_owned(),
                    count,
                    properties: Vec::new(),
                });
            }
            ["property", values @ ..] => {
                let element = elements
                    .last_mut()
                    .ok_or_else(|| invalid("PLY 속성의 항목이 없습니다."))?;
                let (ty, list, name) = match values {
                    [ty, name] => (Scalar::parse(ty)?, None, *name),
                    ["list", count, ty, name] => {
                        (Scalar::parse(ty)?, Some(Scalar::parse(count)?), *name)
                    }
                    _ => return Err(invalid("PLY 속성 구조가 잘못되었습니다.")),
                };
                element.properties.push(Property {
                    name: name.to_owned(),
                    ty,
                    list,
                });
            }
            _ => (),
        }
    }
    if !finished {
        return Err(invalid("PLY 헤더가 너무 큽니다."));
    }
    let bytes = &data[reader.position() as usize..];
    let mut input = match encoding.as_deref() {
        Some("ascii") if bytes.is_ascii() => PlyInput::Ascii(
            std::str::from_utf8(bytes)
                .map_err(io::Error::other)?
                .split_whitespace(),
        ),
        Some("binary_little_endian") => PlyInput::Binary(Cursor::new(bytes), true),
        Some("binary_big_endian") => PlyInput::Binary(Cursor::new(bytes), false),
        _ => return Err(invalid("지원하지 않는 PLY 인코딩입니다.")),
    };
    let mut polygons = Vec::new();
    let mut extras = false;
    for element in elements {
        extras |= !matches!(element.name.as_str(), "vertex" | "face")
            || element.properties.iter().any(|property| {
                !matches!(
                    property.name.as_str(),
                    "x" | "y" | "z" | "vertex_indices" | "vertex_index"
                )
            });
        for i in 0..element.count {
            if i % 1024 == 0 {
                check()?;
            }
            let mut coordinates = [None; 3];
            let mut polygon = Vec::new();
            for property in &element.properties {
                if let Some(count) = property.list {
                    let size = input.scalar(count)?;
                    if !count.integer() || !(0.0..=10000.0).contains(&size) {
                        return Err(invalid("잘못된 PLY 목록 크기입니다."));
                    }
                    let indices = element.name == "face"
                        && matches!(property.name.as_str(), "vertex_indices" | "vertex_index");
                    for _ in 0..size as usize {
                        let index = input.scalar(property.ty)?;
                        if indices {
                            if !property.ty.integer() || index < 0.0 || index > LIMIT as f64 {
                                return Err(invalid("PLY 면 인덱스는 정수여야 합니다."));
                            }
                            polygon.push(index as usize);
                        }
                    }
                } else {
                    let value = input.scalar(property.ty)?;
                    if let Some(i) = ["x", "y", "z"]
                        .iter()
                        .position(|&name| name == property.name)
                    {
                        coordinates[i] = Some(value);
                    }
                }
            }
            if element.name == "vertex" {
                let [Some(x), Some(y), Some(z)] = coordinates else {
                    return Err(invalid("PLY 정점 좌표가 없습니다."));
                };
                mesh.vertices.push([x, y, z]);
            } else if element.name == "face" {
                polygons.push(polygon);
                if polygons.len() > LIMIT {
                    return Err(invalid(MESH_LIMIT));
                }
            }
            mesh.bound()?;
        }
    }
    for polygon in polygons {
        triangulate(&polygon, mesh, check)?;
    }
    if extras {
        warnings.push("PLY 추가 속성(색상·법선·사용자 데이터)은 보존하지 않습니다.".to_owned());
    }
    Ok(())
}

fn read(
    path: &Path,
    format: &str,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<Mesh> {
    let limit = 64 * 1024 * 1024;
    let mut source = File::open(path)?;
    if source.metadata()?.len() > limit {
        return Err(invalid("이 형식은 파일당 64 MiB까지 처리합니다."));
    }
    let mut data = Vec::new();
    let mut block = [0; 65536];
    loop {
        check()?;
        let size = source.read(&mut block)?;
        if size == 0 {
            break;
        }
        data.extend_from_slice(&block[..size]);
        if data.len() as u64 > limit {
            return Err(invalid("입력 크기 제한을 초과했습니다."));
        }
    }
    let mut mesh = Mesh::default();
    match format {
        "obj" => obj(&data, &mut mesh, check, warnings)?,
        "stl" => stl(&data, &mut mesh, check)?,
        "ply" => ply(&data, &mut mesh, check, warnings)?,
        _ => return Err(invalid("지원하지 않는 메시 형식입니다.")),
    }
    mesh.validate(check)?;
    Ok(mesh)
}

fn decimal(value: f64) -> String {
    let scientific = format!("{value:.16e}");
    let (mantissa, exponent) = scientific.split_once('e').unwrap();
    let exponent: i32 = exponent.parse().unwrap();
    let trim = |text: &str| -> String {
        if text.contains('.') {
            text.trim_end_matches('0').trim_end_matches('.').to_owned()
        } else {
            text.to_owned()
        }
    };
    if (-4..17).contains(&exponent) {
        trim(&format!(
            "{value:.digits$}",
            digits = (16 - exponent) as usize
        ))
    } else {
        format!("{}e{exponent:+03}", trim(mantissa))
    }
}

fn write(mesh: &Mesh, target: &Path, format: &str, check: Check<'_>) -> io::Result<()> {
    let mut output = io::BufWriter::new(File::create(target)?);
    if format == "stl" {
        let mut header = [0; 80];
        header[..24].copy_from_slice(b"Magic Converter geometry");
        output.write_all(&header)?;
        output.write_all(&(mesh.faces.len() as u32).to_le_bytes())?;
        for &[i, j, k] in &mesh.faces {
            check()?;
            let (a, b, c) = (mesh.vertices[i], mesh.vertices[j], mesh.vertices[k]);
            let u: Point = std::array::from_fn(|index| b[index] - a[index]);
            let v: Point = std::array::from_fn(|index| c[index] - a[index]);
            let normal: Point = std::array::from_fn(|i| {
                u[(i + 1) % 3] * v[(i + 2) % 3] - u[(i + 2) % 3] * v[(i + 1) % 3]
            });
            let length = normal.iter().map(|x| x * x).sum::<f64>().sqrt();
            let normal = if length == 0.0 {
                [0.0; 3]
            } else {
                normal.map(|x| x / length)
            };
            for value in [normal, a, b, c].into_iter().flatten() {
                let value = value as f32;
                if !value.is_finite() {
                    return Err(invalid("STL float32 좌표 범위를 벗어났습니다."));
                }
                output.write_all(&value.to_le_bytes())?;
            }
            output.write_all(&[0; 2])?;
        }
    } else {
        let obj = format == "obj";
        if !obj {
            write!(
                output,
                "ply\nformat ascii 1.0\nelement vertex {}\nproperty double x\nproperty double y\nproperty double z\nelement face {}\nproperty list uchar int vertex_indices\nend_header\n",
                mesh.vertices.len(),
                mesh.faces.len()
            )?;
        }
        for &[x, y, z] in &mesh.vertices {
            check()?;
            writeln!(
                output,
                "{}{} {} {}",
                if obj { "v " } else { "" },
                decimal(x),
                decimal(y),
                decimal(z)
            )?;
        }
        let offset = usize::from(obj);
        for &[a, b, c] in &mesh.faces {
            check()?;
            writeln!(
                output,
                "{} {} {} {}",
                if obj { "f" } else { "3" },
                a + offset,
                b + offset,
                c + offset
            )?;
        }
    }
    output.flush()
}

pub fn convert(
    source: &Path,
    target: &Path,
    input: &str,
    output: &str,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mesh = read(source, input, check, warnings)?;
    warnings.push("삼각형 좌표·방향만 보존합니다. 개체 계층·재질·텍스처·단위 정보는 출력하지 않습니다. STL은 float32 좌표입니다.".to_owned());
    write(&mesh, target, output, check)?;
    if read(target, output, check, &mut Vec::new())?.faces.len() != mesh.faces.len() {
        return Err(invalid("출력 메시의 삼각형 수 검증에 실패했습니다."));
    }
    Ok(())
}
