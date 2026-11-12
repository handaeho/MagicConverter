"""Render supported static SVG to PDF paths and text using Qt's paint device."""

import math
from pathlib import Path
import re
import tempfile

from defusedxml.ElementTree import fromstring

from magic_converter.core.jobs.job_control import check_cancelled


_SVG = "{http://www.w3.org/2000/svg}"
_ELEMENTS = frozenset("svg g defs title desc metadata style path rect circle ellipse line polyline polygon "
                      "text tspan linearGradient radialGradient stop image use".split())
_UNIT_PIXELS = {"": 1, "px": 1, "pt": 96 / 72, "pc": 16, "mm": 96 / 25.4,
                "cm": 96 / 2.54, "in": 96, "q": 96 / 101.6}
_LENGTH = re.compile(r"\s*([+\-]?(?:\d+\.?\d*|\.\d+)(?:[eE][+\-]?\d+)?)\s*([a-z%]*)\s*")
_UNSUPPORTED_STYLE = re.compile(r"(?:^|[;{])\s*(?:clip-path|mask|filter|writing-mode|animation(?:-[\w-]+)?)\s*:", re.I)
_URL = re.compile(r"url\(\s*['\"]?([^\s)'\"]+)", re.I)


def _length(raw):
    if raw is None:
        return None
    match = _LENGTH.fullmatch(raw)
    if not match or match[2] not in _UNIT_PIXELS and match[2] != "%":
        raise ValueError(f"SVG PDF에서 지원하지 않는 페이지 크기입니다: {raw}")
    length = float(match[1])
    if not math.isfinite(length) or length <= 0:
        raise ValueError("SVG 페이지 크기는 양수여야 합니다.")
    return None if match[2] == "%" else length * _UNIT_PIXELS[match[2]]


def _check_features(element, *, root=False):
    check_cancelled()
    tag = element.tag.removeprefix(_SVG)
    if tag not in _ELEMENTS or tag == "svg" and not root:
        raise ValueError(f"SVG PDF에서 지원하지 않는 요소입니다: {tag}")
    if tag == "metadata":
        return
    for key, value in element.attrib.items():
        attribute = key.rsplit("}", 1)[-1]
        if attribute in {"clip-path", "mask", "filter", "textLength", "lengthAdjust", "writing-mode"}:
            if value != "none":
                raise ValueError(f"SVG PDF에서 지원하지 않는 속성입니다: {attribute}")
        if attribute == "href" and not value.startswith(("#", "data:image/png;", "data:image/jpeg;")):
            raise ValueError("외부 파일을 참조하는 SVG는 PDF로 변환할 수 없습니다.")
    style = element.text or "" if tag == "style" else element.get("style", "")
    if _UNSUPPORTED_STYLE.search(style) or "@import" in style.lower():
        raise ValueError("SVG PDF에서 지원하지 않는 CSS 효과 또는 외부 참조가 있습니다.")
    for value in (*element.attrib.values(), style):
        if any(not reference.startswith("#") for reference in _URL.findall(value)):
            raise ValueError("외부 리소스를 참조하는 SVG는 PDF로 변환할 수 없습니다.")
    for child in element:
        _check_features(child)


def write_svg_pdf(source, destination, notice):
    from PySide6.QtCore import QByteArray, QMarginsF, QRectF, QSizeF, Qt
    from PySide6.QtGui import QPageLayout, QPageSize, QPainter, QPdfWriter
    from PySide6.QtSvg import QSvgRenderer
    from pypdf import PdfReader, PdfWriter, Transformation

    with source.open("rb") as stream:
        data = stream.read(32 * 1024 * 1024 + 1)
    if len(data) > 32 * 1024 * 1024:
        raise ValueError("SVG PDF 입력의 32MiB 한도를 초과했습니다.")
    root = fromstring(data, forbid_dtd=True)
    if root.tag not in {_SVG + "svg", "svg"}:
        raise ValueError("유효한 SVG 파일이 아닙니다.")
    _check_features(root, root=True)
    width, height = (_length(root.get(name)) for name in ("width", "height"))
    view_box = root.get("viewBox")
    if view_box:
        x, y, view_width, view_height = map(float, re.split(r"[\s,]+", view_box.strip()))
        if not all(map(math.isfinite, (x, y, view_width, view_height))) or min(view_width, view_height) <= 0:
            raise ValueError("SVG viewBox가 올바르지 않습니다.")
        if width is None or height is None:
            width = width if width is not None else (height * view_width / view_height if height else view_width)
            height = height if height is not None else width * view_height / view_width
            notice("SVG에 고정 크기가 없는 방향은 viewBox 비율로 PDF 쪽 크기를 정합니다.")
    elif width is not None and height is not None:
        x, y, view_width, view_height = 0, 0, width, height
    else:
        raise ValueError("SVG PDF 쪽 크기에 고정 너비·높이 또는 viewBox가 필요합니다.")
    width_pt, height_pt = width * .75, height * .75
    if min(width_pt, height_pt) < 1 or max(width_pt, height_pt) > 14400 or width * height > 80_000_000:
        raise ValueError("SVG PDF 쪽 크기가 지원 범위를 벗어났습니다.")

    bounds = QRectF(0, 0, width, height)
    aspect = root.get("preserveAspectRatio", "xMidYMid").split()
    if aspect != ["none"]:
        if not aspect or not re.fullmatch(r"x(Min|Mid|Max)Y(Min|Mid|Max)", aspect[0]) or aspect[1:] not in ([], ["meet"], ["slice"]):
            raise ValueError("SVG preserveAspectRatio가 올바르지 않습니다.")
        scale = (max if aspect[1:] == ["slice"] else min)(width / view_width, height / view_height)
        rendered_width, rendered_height = view_width * scale, view_height * scale
        align = {"Min": 0, "Mid": .5, "Max": 1}
        bounds = QRectF((width - rendered_width) * align[aspect[0][1:4]],
                        (height - rendered_height) * align[aspect[0][5:8]], rendered_width, rendered_height)
    renderer = QSvgRenderer(QByteArray(data))
    if not renderer.isValid():
        raise ValueError("유효한 SVG 파일이 아닙니다.")
    renderer.setViewBox(QRectF(x, y, view_width, view_height))
    renderer.setAspectRatioMode(Qt.AspectRatioMode.IgnoreAspectRatio)

    with tempfile.TemporaryDirectory(prefix=".magic-svg-", dir=destination.parent) as folder:
        temporary = Path(folder) / "rendered.pdf"
        writer = QPdfWriter(str(temporary))
        writer.setResolution(720)
        page_size = QPageSize(QSizeF(width_pt, height_pt), QPageSize.Unit.Point, "", QPageSize.SizeMatchPolicy.ExactMatch)
        if not writer.setPageSize(page_size) or not writer.setPageMargins(QMarginsF(), QPageLayout.Unit.Point):
            raise ValueError("SVG PDF 쪽 크기를 설정할 수 없습니다.")
        painter = QPainter(writer)
        if not painter.isActive():
            raise ValueError("SVG PDF 출력 장치를 열 수 없습니다.")
        try:
            painter.scale(writer.width() / width, writer.height() / height)
            painter.setClipRect(QRectF(0, 0, width, height))
            renderer.render(painter, bounds)
        finally:
            painter.end()
        del writer
        check_cancelled()
        with temporary.open("rb") as stream:
            page, = PdfReader(stream).pages
            actual = float(page.mediabox.width), float(page.mediabox.height)
            if actual != (width_pt, height_pt):
                # Qt rounds the MediaBox to whole points. Scale the existing
                # drawing and text together to restore the original dimensions.
                page.add_transformation(Transformation().scale(width_pt / actual[0], height_pt / actual[1]))
                page.mediabox.upper_right = (width_pt, height_pt)
                adjusted = PdfWriter()
                adjusted.add_page(page)
                adjusted.write(destination)
                return
        temporary.replace(destination)
