"""Geometry-only OBJ / STL / PLY interchange, with checked polygon triangulation."""

import io
import math
import struct
from dataclasses import dataclass
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.jobs.job_control import check_cancelled
from magic_converter.core.specialized.special_io import read_bytes, finite


@dataclass
class Mesh:
    vertices: list
    faces: list

    def validate(self):
        if (
            not self.vertices
            or not self.faces
            or len(self.vertices) > 1000000
            or len(self.faces) > 1000000
        ):
            raise ConversionError(
                "비어 있지 않은 삼각형 메시가 필요합니다 (정점/면 각 100만 개까지)."
            )
        if any(
            len(v) != 3 or any(not math.isfinite(x) for x in v) for v in self.vertices
        ):
            raise ConversionError("잘못된 메시 좌표입니다.")
        for i, face in enumerate(self.faces):
            if i % 1024 == 0:
                check_cancelled()
            if len(face) != 3 or any(
                type(x) is not int or x < 0 or x >= len(self.vertices) for x in face
            ):
                raise ConversionError("잘못된 메시 면 인덱스입니다.")
            if len(set(face)) != 3:
                raise ConversionError("중복 정점으로 이루어진 면입니다.")


def triangles(indices, vertices):
    if (
        len(indices) < 3
        or len(indices) > 10000
        or len(set(indices)) != len(indices)
        or any(i < 0 or i >= len(vertices) for i in indices)
    ):
        raise ConversionError("잘못된 다각형 인덱스입니다.")
    if len(indices) == 3:
        return [tuple(indices)]
    # Work near the origin: a large model/world offset must not weaken the
    # planarity tolerance or cancel the polygon's signed area numerically.
    origin = vertices[indices[0]]
    points = [tuple(vertices[i][j] - origin[j] for j in range(3)) for i in indices]
    normal = [
        sum(
            (p[(j + 1) % 3] - q[(j + 1) % 3]) * (p[(j + 2) % 3] + q[(j + 2) % 3])
            for p, q in zip(points, points[1:] + points[:1])
        )
        for j in range(3)
    ]
    length = math.sqrt(sum(x * x for x in normal))
    if length < 1e-15:
        raise ConversionError("퇴화하거나 교차하는 다각형입니다.")
    scale = max(1.0, max(abs(x) for p in points for x in p))
    if any(
        abs(sum(normal[j] * (p[j] - points[0][j]) for j in range(3)))
        > length * scale * 1e-7
        for p in points
    ):
        raise ConversionError("비평면 다각형은 먼저 삼각형으로 분할하세요.")
    axis = max(range(3), key=lambda j: abs(normal[j]))
    pts = [tuple(v[j] for j in range(3) if j != axis) for v in points]

    def cross(a, b, c):
        return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])

    # Crossing edges must not be accepted as a plausible triangulation.
    for i in range(len(pts)):
        check_cancelled()
        for j in range(i + 2, len(pts)):
            if i == 0 and j == len(pts) - 1:
                continue
            a, b, c, d = (
                pts[i],
                pts[(i + 1) % len(pts)],
                pts[j],
                pts[(j + 1) % len(pts)],
            )
            if (
                cross(a, b, c) * cross(a, b, d) < 0
                and cross(c, d, a) * cross(c, d, b) < 0
            ):
                raise ConversionError("교차하는 다각형은 변환할 수 없습니다.")
    area = sum(a[0] * b[1] - b[0] * a[1] for a, b in zip(pts, pts[1:] + pts[:1]))
    sign = 1 if area > 0 else -1
    pending, result = list(range(len(indices))), []
    while len(pending) > 3:
        check_cancelled()
        for n, b in enumerate(pending):
            a, c = pending[n - 1], pending[(n + 1) % len(pending)]
            if sign * cross(pts[a], pts[b], pts[c]) <= 1e-14:
                continue
            if any(
                all(
                    sign * cross(pts[x], pts[y], pts[p]) >= -1e-14
                    for x, y in ((a, b), (b, c), (c, a))
                )
                for p in pending
                if p not in (a, b, c)
            ):
                continue
            result.append((indices[a], indices[b], indices[c]))
            pending.pop(n)
            break
        else:
            raise ConversionError(
                "다각형 삼각분할에 실패했습니다. 입력 메시를 확인하세요."
            )
    return result + [tuple(indices[p] for p in pending)]


def read_mesh(path, kind, notice=lambda _: None):
    data = read_bytes(path)
    vertices, faces = [], []
    if kind == "obj":
        discarded = set()
        for line_no, line in enumerate(data.decode("utf-8-sig").splitlines()):
            if line_no % 1024 == 0:
                check_cancelled()
            parts = line.partition("#")[0].split()
            if not parts:
                continue
            if parts[0] == "v":
                if len(parts) != 4:
                    raise ConversionError(
                        "OBJ는 XYZ 정점만 지원합니다. 정점 색상·동차 좌표는 먼저 정규화하세요."
                    )
                vertices.append(tuple(finite(v) for v in parts[1:]))
            elif parts[0] == "f":
                raw = [int(p.split("/")[0]) for p in parts[1:]]
                if any(x == 0 for x in raw):
                    raise ConversionError("OBJ 인덱스 0은 유효하지 않습니다.")
                faces.extend(
                    triangles(
                        [x - 1 if x > 0 else len(vertices) + x for x in raw], vertices
                    )
                )
                if any("/" in p for p in parts[1:]):
                    discarded.add("UV/법선")
            elif parts[0] not in {"o", "g", "s"}:
                discarded.add(parts[0])
        if discarded:
            notice(
                "OBJ의 재질·텍스처·법선·선/점 개체는 출력하지 않습니다: "
                + ", ".join(sorted(discarded))
            )
    elif kind == "stl":
        if (
            len(data) >= 84
            and len(data) == 84 + 50 * struct.unpack_from("<I", data, 80)[0]
        ):
            count = struct.unpack_from("<I", data, 80)[0]
            for i in range(count):
                if i % 1024 == 0:
                    check_cancelled()
                row = struct.unpack_from("<12fH", data, 84 + i * 50)
                first = len(vertices)
                vertices.extend(tuple(row[j : j + 3]) for j in (3, 6, 9))
                faces.append((first, first + 1, first + 2))
        else:
            lines = [
                line.strip().split()
                for line in data.decode("ascii").splitlines()
                if line.strip()
            ]
            if (
                not lines
                or lines[0][0].lower() != "solid"
                or lines[-1][0].lower() != "endsolid"
            ):
                raise ConversionError("올바른 ASCII/바이너리 STL이 아닙니다.")
            n = 1
            while n < len(lines) - 1:
                check_cancelled()
                block = lines[n : n + 7]
                if (
                    len(block) != 7
                    or block[0][:2] != ["facet", "normal"]
                    or block[1] != ["outer", "loop"]
                    or block[5] != ["endloop"]
                    or block[6] != ["endfacet"]
                    or any(len(x) != 4 or x[0] != "vertex" for x in block[2:5])
                ):
                    raise ConversionError("STL 면 구조가 잘못되었습니다.")
                first = len(vertices)
                vertices.extend(tuple(finite(v) for v in row[1:]) for row in block[2:5])
                faces.append((first, first + 1, first + 2))
                n += 7
    elif kind == "ply":
        stream = io.BytesIO(data)
        if stream.readline().strip() != b"ply":
            raise ConversionError("PLY 헤더가 없습니다.")
        fmt, elements = None, []
        for _ in range(10000):
            line = stream.readline().decode("ascii").strip().split()
            if not line:
                raise ConversionError("PLY 헤더가 중단되었습니다.")
            if line[0] == "end_header":
                break
            if line[0] == "format":
                fmt = line[1]
            elif line[0] == "element":
                count = int(line[2])
                if count < 0 or count > 1000000:
                    raise ConversionError("PLY 항목 개수 제한을 초과했습니다.")
                elements.append((line[1], count, []))
            elif line[0] == "property":
                elements[-1][2].append(line[1:])
        else:
            raise ConversionError("PLY 헤더가 너무 큽니다.")
        types = {
            "char": "b",
            "int8": "b",
            "uchar": "B",
            "uint8": "B",
            "short": "h",
            "int16": "h",
            "ushort": "H",
            "uint16": "H",
            "int": "i",
            "int32": "i",
            "uint": "I",
            "uint32": "I",
            "float": "f",
            "float32": "f",
            "double": "d",
            "float64": "d",
        }
        tokens = iter(stream.read().decode("ascii").split()) if fmt == "ascii" else None
        if fmt not in {"ascii", "binary_little_endian", "binary_big_endian"}:
            raise ConversionError("지원하지 않는 PLY 인코딩입니다.")

        def scalar(t):
            if t not in types:
                raise ConversionError("지원하지 않는 PLY 속성 자료형입니다.")
            if tokens is not None:
                raw = next(tokens)
                return finite(raw) if types[t] in "fd" else int(raw)
            code = ("<" if fmt == "binary_little_endian" else ">") + types[t]
            return struct.unpack(code, stream.read(struct.calcsize(code)))[0]

        polygons = []
        extras = False
        for name, count, props in elements:
            extras |= name not in {"vertex", "face"} or any(
                p[-1] not in {"x", "y", "z", "vertex_indices", "vertex_index"}
                for p in props
            )
            for i in range(count):
                if i % 1024 == 0:
                    check_cancelled()
                values = {}
                for prop in props:
                    if prop[0] == "list":
                        size = scalar(prop[1])
                        if type(size) is not int or size < 0 or size > 10000:
                            raise ConversionError("잘못된 PLY 목록 크기입니다.")
                        values[prop[-1]] = [scalar(prop[2]) for _ in range(size)]
                    else:
                        values[prop[-1]] = scalar(prop[0])
                if name == "vertex":
                    vertices.append(tuple(finite(values[k]) for k in ("x", "y", "z")))
                elif name == "face":
                    polygons.append(
                        values.get("vertex_indices", values.get("vertex_index", []))
                    )
        for polygon in polygons:
            if any(type(i) is not int for i in polygon):
                raise ConversionError("PLY 면 인덱스는 정수여야 합니다.")
            faces.extend(triangles(polygon, vertices))
        if extras:
            notice("PLY 추가 속성(색상·법선·사용자 데이터)은 보존하지 않습니다.")
    else:
        raise ConversionError("지원하지 않는 메시 형식입니다.")
    result = Mesh(vertices, faces)
    result.validate()
    return result


def write_mesh(mesh, path, kind):
    mesh.validate()
    if kind in {"obj", "ply"}:
        with path.open("w", encoding="ascii", newline="\n") as stream:
            if kind == "ply":
                stream.write(
                    f"ply\nformat ascii 1.0\nelement vertex {len(mesh.vertices)}\nproperty double x\nproperty double y\nproperty double z\nelement face {len(mesh.faces)}\nproperty list uchar int vertex_indices\nend_header\n"
                )
            for i, vertex in enumerate(mesh.vertices):
                if i % 1024 == 0:
                    check_cancelled()
                stream.write(
                    ("v " if kind == "obj" else "")
                    + " ".join(format(v, ".17g") for v in vertex)
                    + "\n"
                )
            for i, face in enumerate(mesh.faces):
                if i % 1024 == 0:
                    check_cancelled()
                stream.write(
                    ("f " if kind == "obj" else "3 ")
                    + " ".join(str(v + (1 if kind == "obj" else 0)) for v in face)
                    + "\n"
                )
    elif kind == "stl":
        with path.open("wb") as stream:
            stream.write(
                b"Magic Converter geometry".ljust(80, b"\0")
                + struct.pack("<I", len(mesh.faces))
            )
            for i, face in enumerate(mesh.faces):
                if i % 1024 == 0:
                    check_cancelled()
                a, b, c = [mesh.vertices[j] for j in face]
                u, v = [b[j] - a[j] for j in range(3)], [c[j] - a[j] for j in range(3)]
                normal = [
                    u[1] * v[2] - u[2] * v[1],
                    u[2] * v[0] - u[0] * v[2],
                    u[0] * v[1] - u[1] * v[0],
                ]
                length = math.sqrt(sum(x * x for x in normal))
                stream.write(
                    struct.pack(
                        "<12fH",
                        *(x / length if length else 0 for x in normal),
                        *a,
                        *b,
                        *c,
                        0,
                    )
                )
    else:
        raise ConversionError("지원하지 않는 메시 출력입니다.")


def convert(source, destination, source_kind, target_kind, notice):
    mesh = read_mesh(source, source_kind, notice)
    notice(
        "삼각형 좌표·방향만 보존합니다. 개체 계층·재질·텍스처·단위 정보는 출력하지 않습니다. STL은 float32 좌표입니다."
    )
    write_mesh(mesh, destination, target_kind)
    checked = read_mesh(destination, target_kind)
    if len(checked.faces) != len(mesh.faces):
        raise ConversionError("출력 메시의 삼각형 수 검증에 실패했습니다.")
