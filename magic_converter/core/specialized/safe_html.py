"""A small, inert HTML vocabulary shared by mail and book exports."""

import base64
import io
from urllib.parse import urlsplit
from bs4 import BeautifulSoup
from PIL import Image
from magic_converter.core.conversion.converter import ConversionError

ALLOWED = set(
    "p div span br hr strong b em i u s sub sup h1 h2 h3 h4 h5 h6 blockquote pre code ul ol li table thead tbody tfoot tr td th a img".split()
)
MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp", "image/bmp"}
CSP = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'"


def _image_mime(data):
    with Image.open(io.BytesIO(data)) as image:
        if image.width * image.height > 80_000_000:
            raise ConversionError("내장 이미지는 80 MP까지 처리합니다.")
        if image.format not in {"PNG", "JPEG", "GIF", "WEBP", "BMP"}:
            raise ConversionError("지원하지 않는 내장 전자책/메일 이미지입니다.")
        image.verify()
        return Image.MIME[image.format]


def image_uri(data, mime=None):
    actual = _image_mime(data)
    return "data:" + actual + ";base64," + base64.b64encode(data).decode("ascii")


def decode_image(uri):
    try:
        header, payload = uri.split(",", 1)
        mime = header[5:].split(";")[0].lower()
        if (
            not header.startswith("data:")
            or mime not in MIMES
            or not header.endswith(";base64")
            or len(payload) > 90 * 1024 * 1024
        ):
            raise ValueError()
        data = base64.b64decode(payload, validate=True)
        if _image_mime(data) != mime:
            raise ConversionError(
                "내장 이미지의 MIME 선언과 실제 파일 형식이 다릅니다."
            )
        return data, mime
    except (ValueError, TypeError) as exc:
        raise ConversionError("잘못된 내장 이미지입니다.") from exc


def sanitize(body, resolver=None, link_resolver=None, *, assets=None):
    soup = BeautifulSoup(body, "html.parser")
    for tag in list(
        soup.find_all(
            [
                "script",
                "style",
                "iframe",
                "object",
                "embed",
                "svg",
                "math",
                "template",
                "head",
                "video",
                "audio",
                "noscript",
            ]
        )
    ):
        tag.decompose()
    for tag in list(soup.find_all(True)):
        if tag.name not in ALLOWED:
            # Retain the destination of inert content without retaining its
            # vocabulary/attributes. Active subtrees were removed above.
            if tag.name in {"section", "aside", "article", "main", "figure", "figcaption", "header", "footer", "nav"}:
                tag.name = "div"
            elif tag.get("id"):
                tag.name = "span"
            else:
                tag.unwrap()
                continue
        attrs = {}
        if tag.name == "img":
            src = str(tag.get("src", ""))
            if not src.startswith(("data:", "mc-asset:")) and resolver:
                src = resolver(src) or ""
            if not src.startswith("data:") and not (assets is not None and src.startswith("mc-asset:")):
                tag.replace_with(soup.new_string("[외부/지원하지 않는 이미지 생략]"))
                continue
            if assets is not None:
                src = assets.intern(src)
            else:
                decode_image(src)
            attrs = {"src": src, "alt": str(tag.get("alt", ""))}
        elif tag.name == "a":
            href = str(tag.get("href", ""))
            resolved = False
            if link_resolver and not urlsplit(href).scheme:
                href = link_resolver(href) or ""
                resolved = bool(href)
            if (
                resolved
                or href.startswith("#")
                or urlsplit(href).scheme.lower()
                in {
                    "https",
                    "http",
                    "mailto",
                }
            ):
                attrs["href"] = href
        elif tag.name in {"td", "th"}:
            for key in ("rowspan", "colspan"):
                if str(tag.get(key, "")).isdigit() and 0 < int(tag[key]) <= 1000:
                    attrs[key] = str(tag[key])
        if str(tag.get("id", "")):
            attrs["id"] = str(tag["id"])
        tag.attrs = attrs
    from bs4 import Comment

    for comment in soup.find_all(string=lambda s: isinstance(s, Comment)):
        comment.extract()
    return "".join(str(c) for c in (soup.body.contents if soup.body else soup.contents))


def page(title, body):
    import html

    return f'<!DOCTYPE html><html><head><meta charset="utf-8"><meta http-equiv="Content-Security-Policy" content="{CSP}"><title>{html.escape(title)}</title></head><body>{body}</body></html>'
