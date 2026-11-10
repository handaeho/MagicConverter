"""Chapter/metadata/image model for EPUB and FB2, plus ordered comic pages.

Reference: Filee Ebooks/Book, EpubReader/Writer, Fb2Reader/Writer, ComicBook.
No DRM removal, external resources, original CSS/layout or executable content.
"""

import base64
import hashlib
import html
import io
import json
import mimetypes
import posixpath
import re
import zipfile
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET
from urllib.parse import quote, unquote, urldefrag
from bs4 import BeautifulSoup, NavigableString
from PIL import Image
from magic_converter.core.conversion.converter import ConversionError
from magic_converter.core.specialized.special_io import Package, read_bytes, text, xml, relative_asset
from magic_converter.core.specialized.safe_html import sanitize, page, image_uri, decode_image
from magic_converter.core.specialized.book_assets import ImageAssets, BookLimitError, OutputBudget, inline_images
from magic_converter.core.jobs.job_control import check_cancelled

OPF = "http://www.idpf.org/2007/opf"
DC = "http://purl.org/dc/elements/1.1/"
XHTML = "http://www.w3.org/1999/xhtml"
FB = "http://www.gribuser.ru/xml/fictionbook/2.0"
XLINK = "http://www.w3.org/1999/xlink"


@dataclass
class Chapter:
    title: str
    body: str


@dataclass
class Book:
    title: str
    authors: list[str] = field(default_factory=list)
    language: str = "und"
    description: str = ""
    publisher: str = ""
    chapters: list[Chapter] = field(default_factory=list)
    cover: str = ""  # image asset reference (legacy input may use a data URI)
    assets: ImageAssets = field(default_factory=ImageAssets)


def append_chapter(book, title, body):
    book.assets.add_text(title)
    book.assets.add_text(body)
    book.chapters.append(Chapter(title, body))


def local(node):
    return node.tag.rsplit("}", 1)[-1]


def read_book(path, kind, notice=lambda _: None):
    if kind == "epub":
        with Package(path) as package:
            if package.read("mimetype").strip() != b"application/epub+zip":
                raise ConversionError("EPUB mimetype이 올바르지 않습니다.")
            container = xml(package.read("META-INF/container.xml"))
            roots = [
                e.get("full-path") for e in container.iter() if local(e) == "rootfile"
            ]
            if len(roots) != 1 or not roots[0]:
                raise ConversionError("단일 rendition EPUB이 필요합니다.")
            opf_path = roots[0]
            opf = xml(package.read(opf_path))
            manifest = {}
            for n in opf.iter():
                if local(n) == "item":
                    if n.get("id") in manifest:
                        raise ConversionError("EPUB manifest ID가 중복됩니다.")
                    manifest[n.get("id")] = n

            from magic_converter.core.specialized.epub_protection import validate
            validate(package, opf_path, manifest, notice)

            def meta(key):
                return [n.text or "" for n in opf.iter(f"{{{DC}}}{key}")]

            book = Book(
                next(iter(meta("title")), path.stem),
                meta("creator"),
                next(iter(meta("language")), "und"),
                next(iter(meta("description")), ""),
                next(iter(meta("publisher")), ""),
            )
            image_refs = {}

            def packaged_image(name):
                if name not in image_refs:
                    image_refs[name] = book.assets.add(package.read(name))
                return image_refs[name]
            covers = [
                n
                for n in manifest.values()
                if "cover-image" in n.get("properties", "").split()
            ]
            for m in opf.iter():
                if (
                    local(m) == "meta"
                    and m.get("name") == "cover"
                    and m.get("content") in manifest
                ):
                    covers.append(manifest[m.get("content")])
            if covers:
                book.cover = packaged_image(relative_asset(opf_path, covers[0].get("href", "")))
            spine_refs = [n for n in opf.iter() if local(n) == "itemref"]
            chapter_indices = {}
            for index, ref in enumerate(spine_refs):
                item = manifest.get(ref.get("idref"))
                if item is not None:
                    chapter_indices.setdefault(
                        relative_asset(opf_path, item.get("href", "")), index
                    )
            for chapter_index, ref in enumerate(spine_refs):
                check_cancelled()
                if ref.get("linear") == "no":
                    notice("EPUB의 linear=no 부록도 장 순서대로 포함합니다.")
                item = manifest.get(ref.get("idref"))
                if item is None or item.get("media-type") not in {
                    "application/xhtml+xml",
                    "text/html",
                }:
                    raise ConversionError("EPUB spine에 지원하지 않는 문서가 있습니다.")
                chapter_path = relative_asset(opf_path, item.get("href", ""))
                content = package.read(chapter_path)
                if item.get("media-type") == "application/xhtml+xml":
                    # Parse namespaced XHTML as XML, then hand a neutral HTML
                    # vocabulary to the shared sanitizer (ns0:p is not p).
                    document = xml(
                        re.sub(rb"<!DOCTYPE\s+html\s*>", b"", content, flags=re.I)
                    )
                    for element in document.iter():
                        element.tag = local(element)
                        element.attrib = {
                            key.rsplit("}", 1)[-1]: v
                            for key, v in element.attrib.items()
                        }
                    raw = ET.tostring(document, encoding="unicode")
                else:
                    raw = content.decode("utf-8-sig")
                soup = BeautifulSoup(raw, "html.parser")
                heading = soup.find(re.compile("^h[1-6]$")) or soup.title
                title = (
                    heading.get_text(" ", strip=True)
                    if heading
                    else f"Chapter {len(book.chapters)+1}"
                )

                def resolver(href):
                    try:
                        return packaged_image(relative_asset(chapter_path, href))
                    except BookLimitError:
                        raise
                    except (ConversionError, KeyError):
                        notice("외부/누락된 EPUB 이미지를 생략했습니다.")
                        return ""

                # Namespace original anchors by source chapter and resolve
                # package-relative links before the generic HTML sanitizer.
                prefix = f"mc{chapter_index}-id-"
                for element in soup.find_all(id=True):
                    element["id"] = prefix + str(element["id"])
                for element in soup.find_all(["html", "body"]):
                    element.name = "div"

                def link_resolver(href):
                    name, fragment = urldefrag(href)
                    target = (
                        relative_asset(chapter_path, name) if name else chapter_path
                    )
                    if target not in chapter_indices:
                        notice("EPUB spine 밖의 내부 링크는 텍스트만 보존했습니다.")
                        return ""
                    target_index = chapter_indices[target]
                    return (
                        "#"
                        # The model stores hrefs as URIs, IDs as literal text.
                        # Decode the incoming URI once, then encode the new URI.
                        + quote(f"mc{target_index}-"
                                + ("id-" + unquote(fragment) if fragment else "start"), safe="")
                    )

                body = sanitize(str(soup), resolver, link_resolver, assets=book.assets)
                body = f'<div id="mc{chapter_index}-start">{body}</div>'
                append_chapter(book, title, body)
    elif kind == "fb2":
        root = xml(read_bytes(path))
        if root.tag != f"{{{FB}}}FictionBook":
            raise ConversionError("FictionBook 2.0 문서가 아닙니다.")
        title_info = root.find(f"{{{FB}}}description/{{{FB}}}title-info")
        if title_info is None:
            raise ConversionError("FB2 title-info가 없습니다.")

        def content(parent, name):
            e = parent.find(f"{{{FB}}}{name}")
            return "".join(e.itertext()).strip() if e is not None else ""

        authors = []
        for a in title_info.findall(f"{{{FB}}}author"):
            authors.append(
                " ".join(
                    filter(
                        None,
                        [
                            content(a, n)
                            for n in (
                                "first-name",
                                "middle-name",
                                "last-name",
                                "nickname",
                            )
                        ],
                    )
                )
            )
        book = Book(
            content(title_info, "book-title") or path.stem,
            authors,
            content(title_info, "lang") or "und",
            content(title_info, "annotation"),
        )
        binaries = {}
        for node in root.findall(f"{{{FB}}}binary"):
            key = node.get("id")
            if not key or key in binaries:
                raise ConversionError("FB2 binary ID가 없거나 중복됩니다.")
            data = base64.b64decode("".join((node.text or "").split()), validate=True)
            binaries[key] = book.assets.add(data)
        publish = root.find(f"{{{FB}}}description/{{{FB}}}publish-info")
        if publish is not None:
            book.publisher = content(publish, "publisher")
        cover = title_info.find(f"{{{FB}}}coverpage/{{{FB}}}image")
        if cover is not None:
            key = cover.get(f"{{{XLINK}}}href", "").lstrip("#")
            if key not in binaries:
                raise ConversionError("FB2 표지 이미지가 누락되었습니다.")
            book.cover = binaries[key]
        mapping = {
            "section": "div",
            "title": "h2",
            "subtitle": "h3",
            "strong": "strong",
            "emphasis": "em",
            "strikethrough": "s",
            "empty-line": "br",
            "image": "img",
            "a": "a",
            "poem": "div",
            "stanza": "div",
            "v": "p",
            "text-author": "p",
        }

        def render(node, heading=False):
            tag = mapping.get(local(node), local(node))
            identity = (
                ' id="' + html.escape(node.get("id"), quote=True) + '"'
                if node.get("id")
                else ""
            )
            # FB2 titles contain paragraphs; XHTML headings allow phrasing
            # content only. Keep their boundaries and inline markup intact.
            if local(node) == "title":
                body = html.escape(node.text or "") + "".join(
                    render(c, heading=True) + html.escape(c.tail or "") for c in node
                )
                return f"<h2{identity}>{body}</h2>"
            if heading and tag == "p":
                body = html.escape(node.text or "") + "".join(
                    render(c, heading=True) + html.escape(c.tail or "") for c in node
                )
                return (
                    f"<span{identity}>{body}</span><br/>"
                    if identity
                    else body + "<br/>"
                )
            attrs = ""
            if tag == "img":
                key = node.get(f"{{{XLINK}}}href", "").lstrip("#")
                if key not in binaries:
                    raise ConversionError("FB2 본문 이미지가 누락되었습니다.")
                return "<img" + identity + ' src="' + binaries[key] + '"/>'
            if tag == "a":
                attrs = (
                    ' href="'
                    + html.escape(node.get(f"{{{XLINK}}}href", ""), quote=True)
                    + '"'
                )
            if tag in {"td", "th"}:
                attrs = "".join(
                    f' {k}="{html.escape(node.get(k),quote=True)}"'
                    for k in ("rowspan", "colspan")
                    if node.get(k)
                )
            body = html.escape(node.text or "") + "".join(
                render(c) + html.escape(c.tail or "") for c in node
            )
            return (
                f"<{tag}{identity}{attrs}>{body}</{tag}>"
                if tag != "br"
                else f"<br{identity}/>"
            )

        for body in root.findall(f"{{{FB}}}body"):
            sections = list(body)
            if not sections:
                continue
            # Named bodies (notes) are included, never silently discarded.
            for section in sections:
                check_cancelled()
                title = content(section, "title") or f"Chapter {len(book.chapters)+1}"
                append_chapter(book, title, sanitize(render(section), assets=book.assets))
    elif kind in {"txt", "md", "docx"}:
        if kind == "docx":
            from magic_converter.core.documents.hwp_documents import read_docx, document_html

            raw = document_html(read_docx(path))
        elif kind == "md":
            import markdown

            raw = markdown.markdown(text(path), extensions=["tables", "fenced_code"])
        else:
            raw = "".join(
                "<p>" + html.escape(line) + "</p>" for line in text(path).splitlines()
            )
        book = Book(path.stem)
        append_chapter(book, path.stem, sanitize(raw, assets=book.assets))
        notice(
            "문서의 본문을 한 장으로 가져옵니다. 외부 이미지·페이지 배치·머리말·꼬리말·편집 수식은 보존하지 않습니다."
        )
    else:
        raise ConversionError("지원하지 않는 전자책 입력입니다.")
    if not book.chapters:
        raise ConversionError("전자책에 읽을 장이 없습니다.")
    book.assets.check_model(book)
    notice(
        "전자책의 기본 메타데이터·장 순서·기본 강조/표·내장 래스터 이미지를 공통 모델로 읽었습니다. EPUB의 기본 내부 링크를 보존하며, 원래 CSS·페이지 배치·복잡한 주석 동작·스크립트·오디오/동영상·폰트는 보존하지 않습니다. 출력별 보존 범위가 다릅니다."
    )
    return book


def _html_xml(parent, body, image_callback):
    soup = BeautifulSoup(body, "html.parser")

    def build(parent, node):
        if isinstance(node, NavigableString):
            if len(parent):
                parent[-1].tail = (parent[-1].tail or "") + str(node)
            else:
                parent.text = (parent.text or "") + str(node)
            return
        if not getattr(node, "name", None):
            return
        attrs = dict(node.attrs)
        if node.name == "img":
            attrs["src"] = image_callback(attrs["src"])
        child = ET.SubElement(parent, f"{{{XHTML}}}{node.name}", attrs)
        for c in node.children:
            build(child, c)

    for n in soup.contents:
        build(parent, n)


def _epub_documents(book):
    book.assets.check_model(book)
    documents, locations = [], {}
    for index, chapter in enumerate(book.chapters):
        soup = BeautifulSoup(
            sanitize(chapter.body, link_resolver=lambda href: href, assets=book.assets), "html.parser"
        )
        ids = set()
        for element in soup.find_all(id=True):
            identity = str(element["id"])
            if not identity or any(c.isspace() for c in identity) or identity in ids:
                raise ConversionError(
                    "EPUB 장의 ID가 비어 있거나 중복/공백을 포함합니다."
                )
            ids.add(identity)
            locations.setdefault(identity, []).append(index)
        documents.append((soup, ids))
    for index, (soup, ids) in enumerate(documents):
        for anchor in soup.find_all("a", href=True):
            href = anchor["href"]
            if not href.startswith("#"):
                # Relative links produced by our own EPUB serializer are
                # validated too; arbitrary relative files are not exported.
                match = re.fullmatch(r"chapter([0-9]+)\.xhtml(?:#(.*))?", href)
                if not match:
                    if not re.match(r"^(?:https?|mailto):", href, re.I):
                        del anchor["href"]
                    continue
                target, fragment = int(match[1]), unquote(match[2] or "")
                if (
                    target >= len(documents)
                    or fragment
                    and fragment not in documents[target][1]
                ):
                    raise ConversionError("EPUB 내부 링크의 대상이 없습니다.")
            else:
                fragment = unquote(href[1:])
                if not fragment:
                    target = index
                elif fragment in ids:
                    target = index
                else:
                    candidates = locations.get(fragment, [])
                    if len(candidates) != 1:
                        raise ConversionError(
                            "EPUB 내부 링크의 대상이 없거나 여러 장에서 모호합니다."
                        )
                    target = candidates[0]
            anchor["href"] = (
                "" if target == index and fragment else f"chapter{target}.xhtml"
            ) + ("#" + quote(fragment, safe="") if fragment else "")
    book.assets.check_model(book)
    return [str(soup) for soup, _ in documents]


def write_epub(book, path, notice=lambda _: None):
    documents = _epub_documents(book)
    assets = {}
    image_cache = {}

    def add_image(uri):
        uri = book.assets.intern(uri)
        if uri in image_cache:
            return image_cache[uri]
        data, mime = book.assets.resolve(uri)
        if mime == "image/bmp":
            # BMP is not an EPUB core image resource and needs a fallback.
            # Lossless PNG is directly usable by readers and by EPUBCheck.
            with Image.open(io.BytesIO(data)) as image:
                output = io.BytesIO()
                image.save(output, format="PNG")
                data = output.getvalue()
            mime = "image/png"
            notice("EPUB 호환성을 위해 BMP 이미지를 무손실 PNG로 저장했습니다.")
        suffix = {
            "image/png": "png",
            "image/jpeg": "jpg",
            "image/gif": "gif",
            "image/webp": "webp",
        }[mime]
        name = "images/" + hashlib.sha256(data).hexdigest() + "." + suffix
        assets[name] = (data, mime)
        image_cache[uri] = name
        return name

    cover = add_image(book.cover) if book.cover else None
    chapters = []
    for i, ch in enumerate(book.chapters):
        check_cancelled()
        root = ET.Element(f"{{{XHTML}}}html", {"lang": book.language})
        head = ET.SubElement(root, f"{{{XHTML}}}head")
        ET.SubElement(head, f"{{{XHTML}}}title").text = ch.title
        body = ET.SubElement(root, f"{{{XHTML}}}body")
        _html_xml(body, documents[i], add_image)
        chapters.append(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    opf = ET.Element(
        f"{{{OPF}}}package", {"version": "3.0", "unique-identifier": "book-id"}
    )
    metadata = ET.SubElement(opf, f"{{{OPF}}}metadata")
    chapter_hash = hashlib.sha256()
    for chapter in chapters:
        chapter_hash.update(chapter)
    identity = "urn:sha256:" + chapter_hash.hexdigest()
    ET.SubElement(metadata, f"{{{DC}}}identifier", id="book-id").text = identity
    for key, values in (
        ("title", [book.title]),
        ("creator", book.authors),
        ("language", [book.language]),
        ("description", [book.description]),
        ("publisher", [book.publisher]),
    ):
        for value in values:
            if value:
                ET.SubElement(metadata, f"{{{DC}}}{key}").text = value
    from datetime import datetime, timezone

    ET.SubElement(metadata, f"{{{OPF}}}meta", property="dcterms:modified").text = (
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )
    manifest = ET.SubElement(opf, f"{{{OPF}}}manifest")
    spine = ET.SubElement(opf, f"{{{OPF}}}spine")
    for i, ch in enumerate(chapters):
        ET.SubElement(
            manifest,
            f"{{{OPF}}}item",
            id=f"c{i}",
            href=f"chapter{i}.xhtml",
            attrib={"media-type": "application/xhtml+xml"},
        )
        ET.SubElement(spine, f"{{{OPF}}}itemref", idref=f"c{i}")
    ET.SubElement(
        manifest,
        f"{{{OPF}}}item",
        id="nav",
        href="nav.xhtml",
        properties="nav",
        attrib={"media-type": "application/xhtml+xml"},
    )
    for i, (name, (_, mime)) in enumerate(assets.items()):
        attrs = {"id": f"image{i}", "href": name, "media-type": mime}
        if name == cover:
            attrs["properties"] = "cover-image"
        ET.SubElement(manifest, f"{{{OPF}}}item", attrs)
    container = b'<?xml version="1.0"?><container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>'
    nav = ET.Element(f"{{{XHTML}}}html")
    ET.SubElement(ET.SubElement(nav, f"{{{XHTML}}}head"), f"{{{XHTML}}}title").text = (
        book.title
    )
    toc = ET.SubElement(
        ET.SubElement(nav, f"{{{XHTML}}}body"),
        f"{{{XHTML}}}nav",
        {"{http://www.idpf.org/2007/ops}type": "toc"},
    )
    listing = ET.SubElement(toc, f"{{{XHTML}}}ol")
    for i, ch in enumerate(book.chapters):
        ET.SubElement(
            ET.SubElement(listing, f"{{{XHTML}}}li"),
            f"{{{XHTML}}}a",
            href=f"chapter{i}.xhtml",
        ).text = ch.title
    budget = OutputBudget()
    for data in [container, ET.tostring(opf, encoding="utf-8", xml_declaration=True),
                 ET.tostring(nav, encoding="utf-8", xml_declaration=True), *chapters]:
        budget.reserve(len(data))
    budget.reserve(len(b"application/epub+zip"))
    for data, _ in assets.values():
        budget.reserve(len(data))
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "mimetype", b"application/epub+zip", compress_type=zipfile.ZIP_STORED
        )
        z.writestr("META-INF/container.xml", container)
        z.writestr(
            "EPUB/package.opf", ET.tostring(opf, encoding="utf-8", xml_declaration=True)
        )
        z.writestr(
            "EPUB/nav.xhtml", ET.tostring(nav, encoding="utf-8", xml_declaration=True)
        )
        for i, ch in enumerate(chapters):
            z.writestr(f"EPUB/chapter{i}.xhtml", ch)
        for name, (data, _) in assets.items():
            check_cancelled()
            z.writestr("EPUB/" + name, data)


def write_fb2(book, path, notice):
    book.assets.check_model(book)
    root = ET.Element(f"{{{FB}}}FictionBook")

    def sub(parent, tag, body=None, **attrs):
        node = ET.SubElement(parent, f"{{{FB}}}{tag}", attrs)
        node.text = body
        return node

    description = sub(root, "description")
    info = sub(description, "title-info")
    sub(info, "genre", "unrecognised")
    for author in book.authors or ["Unknown"]:
        sub(sub(info, "author"), "nickname", author)
    sub(info, "book-title", book.title)
    if book.description:
        sub(sub(info, "annotation"), "p", book.description)
    images = {}

    def image_id(uri):
        data, mime = book.assets.resolve(uri)
        key = "img" + hashlib.sha256(data).hexdigest()
        images[key] = (data, mime)
        return "#" + key

    if book.cover:
        sub(sub(info, "coverpage"), "image").set(
            f"{{{XLINK}}}href", image_id(book.cover)
        )
    sub(info, "lang", book.language)
    docinfo = sub(description, "document-info")
    sub(sub(docinfo, "author"), "nickname", "Magic Converter")
    sub(docinfo, "id", hashlib.sha256(book.title.encode()).hexdigest())
    sub(docinfo, "version", "1.0")
    if book.publisher:
        sub(sub(description, "publish-info"), "publisher", book.publisher)
    body = sub(root, "body")
    inline = {
        "strong": "strong",
        "b": "strong",
        "em": "emphasis",
        "i": "emphasis",
        "sub": "sub",
        "sup": "sup",
        "s": "strikethrough",
        "code": "code",
    }

    def inline_copy(parent, node):
        if isinstance(node, NavigableString):
            if len(parent):
                parent[-1].tail = (parent[-1].tail or "") + str(node)
            else:
                parent.text = (parent.text or "") + str(node)
            return
        if not getattr(node, "name", None):
            return
        tag = node.name
        if tag == "img":
            sub(parent, "image").set(f"{{{XLINK}}}href", image_id(node["src"]))
            return
        if tag in {"br", "hr"}:
            # Within a paragraph, a line break stays literal text.
            inline_copy(parent, NavigableString("\n"))
            return
        target = inline.get(tag)
        if target:
            child = sub(parent, target)
        else:
            child = parent
        for c in node.children:
            inline_copy(child, c)

    def block_copy(parent, node):
        if isinstance(node, NavigableString):
            if str(node).strip():
                inline_copy(sub(parent, "p"), node)
            return
        if not getattr(node, "name", None):
            return
        tag = node.name
        if tag == "table":
            table = sub(parent, "table")
            for row in node.find_all("tr"):
                if row.find_parent("table") is not node:
                    continue
                target_row = sub(table, "tr")
                for cell in row.find_all(["td", "th"], recursive=False):
                    target_cell = sub(target_row, cell.name)
                    for key in ("colspan", "rowspan"):
                        if cell.get(key):
                            target_cell.set(key, cell[key])
                    for child in cell.children:
                        inline_copy(target_cell, child)
        elif tag in {
            "p",
            "li",
            "pre",
            "blockquote",
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
            "h6",
        }:
            target = sub(parent, "p")
            if tag.startswith("h") and tag[1:].isdigit():
                target = sub(target, "strong")
            if tag == "li":
                target.text = "• "
            for child in node.children:
                inline_copy(target, child)
        elif tag == "img":
            inline_copy(parent, node)
        elif tag in {"br", "hr"}:
            sub(parent, "empty-line")
        elif tag in inline:
            inline_copy(sub(parent, "p"), node)
        else:
            for child in node.children:
                block_copy(parent, child)

    for ch in book.chapters:
        check_cancelled()
        section = sub(body, "section")
        sub(sub(section, "title"), "p", ch.title)
        soup = BeautifulSoup(ch.body, "html.parser")
        for node in soup.contents:
            if isinstance(node, NavigableString) and not str(node).strip():
                continue
            block_copy(section, node)
    budget = OutputBudget()
    for key, (data, mime) in images.items():
        budget.reserve(4 * ((len(data) + 2) // 3))
        sub(root, "binary", base64.b64encode(data).decode("ascii"), id=key)
        root[-1].set("content-type", mime)
    notice(
        "FB2에서는 HTML 링크·CSS·목록 서식·일부 HTML 구조를 단순화합니다. 본문 텍스트·표·강조·이미지는 보존합니다."
    )
    data = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    if len(data) > budget.max_bytes:
        raise BookLimitError("전자책 출력 확장 용량 제한을 초과했습니다.")
    path.write_bytes(data)


def _to_docx(book, path):
    from magic_converter.core.documents.hwp_documents import (
        DocumentData,
        Paragraph,
        TextRun,
        Picture,
        Cell,
        Table,
        write_docx,
    )

    blocks = []
    for chapter in book.chapters:
        blocks.append(
            Paragraph(
                [TextRun(chapter.title, 16, True)], page_break_before=bool(blocks)
            )
        )
        soup = BeautifulSoup(chapter.body, "html.parser")

        def runs(node, bold=False, italic=False):
            if isinstance(node, NavigableString):
                return [TextRun(str(node), bold=bold, italic=italic)]
            if node.name == "img":
                data, _ = book.assets.resolve(node["src"])
                with Image.open(io.BytesIO(data)) as img:
                    width = min(400, img.width * 0.75)
                    return [Picture(data, width, width * img.height / img.width)]
            if node.name == "br":
                return [TextRun("\n")]
            return [
                run
                for child in node.children
                for run in runs(
                    child,
                    bold or node.name in {"strong", "b"},
                    italic or node.name in {"em", "i"},
                )
            ]

        def visit(node):
            if not getattr(node, "name", None):
                if str(node).strip():
                    blocks.append(Paragraph(runs(node)))
            elif node.name == "table":
                rows = node.find_all("tr")
                cells = []
                occupied = set()
                for r, row in enumerate(rows):
                    c = 0
                    for cell in row.find_all(["td", "th"], recursive=False):
                        while (r, c) in occupied:
                            c += 1
                        rs, cs = int(cell.get("rowspan", 1)), int(
                            cell.get("colspan", 1)
                        )
                        if r + rs > len(rows) or c + cs > 1000:
                            raise ConversionError(
                                "전자책 표 병합 범위가 올바르지 않습니다."
                            )
                        cells.append(Cell(r, c, [Paragraph(runs(cell))], rs, cs))
                        occupied.update(
                            (rr, cc)
                            for rr in range(r, r + rs)
                            for cc in range(c, c + cs)
                        )
                        c += cs
                if cells:
                    blocks.append(
                        Table(len(rows), max(c.col + c.colspan for c in cells), cells)
                    )
            elif (
                node.name in {"p", "h1", "h2", "h3", "h4", "h5", "h6", "pre", "li"}
                or node.name == "img"
            ):
                blocks.append(Paragraph(runs(node)))
            else:
                for child in node.children:
                    visit(child)

        for node in soup.contents:
            visit(node)
    write_docx(DocumentData(blocks), path)


def comic(source, destination, source_kind, target_kind, notice):
    if source_kind == "pdf":
        import fitz

        with fitz.open(source) as pdf, zipfile.ZipFile(
            destination, "w", zipfile.ZIP_DEFLATED
        ) as z:
            if len(pdf) > 1000:
                raise ConversionError("CBZ는 1000페이지까지 생성합니다.")
            for i, page_ in enumerate(pdf):
                check_cancelled()
                z.writestr(f"{i+1:05}.png", page_.get_pixmap(dpi=144).tobytes("png"))
        notice(
            "PDF를 144 DPI 페이지 이미지로 래스터화했습니다. 검색 텍스트·링크·양식·주석 편집 기능은 포함되지 않습니다."
        )
        return
    with Package(source) as package:
        names = [
            n
            for n in package.names
            if posixpath.splitext(n)[1].lower()
            in {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
        ]

        def natural(name):
            return tuple(
                (0, int(v)) if v.isdigit() else (1, v.casefold())
                for v in re.split(r"(\d+)", name)
            )

        names.sort(key=lambda n: (natural(n), n))
        if not names or len(names) > 1000:
            raise ConversionError("CBZ에 1~1000개의 이미지 페이지가 필요합니다.")
        pages = []
        image_assets = ImageAssets()
        for name in names:
            check_cancelled()
            data = package.read(name)
            with Image.open(io.BytesIO(data)) as img:
                if getattr(img, "n_frames", 1) > 1:
                    notice("만화 페이지의 애니메이션은 첫 프레임만 표시합니다.")
            pages.append((name, data, image_assets.add(data)))
    if target_kind == "pdf":
        import fitz

        with fitz.open() as pdf:
            for name, data, uri in pages:
                check_cancelled()
                with Image.open(io.BytesIO(data)) as img:
                    rgb = img.convert("RGB")
                    buffer = io.BytesIO()
                    rgb.save(buffer, format="PNG")
                    page_ = pdf.new_page(
                        width=img.width * 0.75, height=img.height * 0.75
                    )
                    page_.insert_image(page_.rect, stream=buffer.getvalue())
            pdf.save(destination)
    else:
        book = Book(
            source.stem,
            chapters=[
                Chapter(
                    name,
                    f'<p><img src="{uri}" alt="{html.escape(name,quote=True)}"/></p>',
                )
                for name, data, uri in pages
            ],
            assets=image_assets,
        )
        write_epub(book, destination, notice)
        notice(
            "CBZ를 이미지 중심의 reflowable EPUB으로 저장했습니다. 고정 배치 EPUB은 아닙니다."
        )
    notice(
        "CBZ 페이지는 파일명 자연 정렬 순서입니다. ComicInfo 메타데이터·책갈피·원래 폴더는 보존하지 않습니다."
    )


def convert(source, destination, source_kind, target_kind, notice):
    if source_kind in {"cbz", "pdf"}:
        return comic(source, destination, source_kind, target_kind, notice)
    book = read_book(source, source_kind, notice)
    if target_kind == "epub":
        write_epub(book, destination, notice)
    elif target_kind == "fb2":
        write_fb2(book, destination, notice)
    elif target_kind == "html":
        budget = OutputBudget()
        opening, closing = page(book.title, "").rsplit("</body>", 1)
        with destination.open("w", encoding="utf-8", newline="") as stream:
            budget.write(stream, opening)
            for chapter in book.chapters:
                budget.write(stream, "<section><h1>" + html.escape(chapter.title) + "</h1>")
                stream.write(inline_images(book, chapter.body, budget))
                budget.write(stream, "</section>")
            budget.write(stream, "</body>" + closing)
        notice(
            "HTML에는 제목과 장 본문을 저장합니다. 저자·언어·설명·출판사 메타데이터와 별도 표지 지정은 포함하지 않습니다."
        )
    elif target_kind == "txt":
        budget = OutputBudget()
        with destination.open("w", encoding="utf-8", newline="") as stream:
            for index, chapter in enumerate(book.chapters):
                budget.write(stream, ("\n\n" if index else "") + chapter.title + "\n")
                budget.write(stream, BeautifulSoup(chapter.body, "html.parser").get_text("\n", strip=True))
        notice("TXT에는 이미지·표 구조·강조·메타데이터를 포함하지 않습니다.")
    elif target_kind == "md":
        budget = OutputBudget()
        with destination.open("w", encoding="utf-8", newline="") as stream:
            for index, chapter in enumerate(book.chapters):
                budget.write(stream, ("\n\n" if index else "") + "# " + chapter.title + "\n\n")
                stream.write(inline_images(book, chapter.body, budget))
        notice(
            "Markdown에 장 제목·기본 HTML 본문·data URI 이미지를 포함했습니다. 책 메타데이터와 별도 표지 지정은 제외하며, 렌더러에 따라 HTML/이미지 표시가 제한될 수 있습니다."
        )
    elif target_kind == "docx":
        _to_docx(book, destination)
        from docx import Document

        document = Document(destination)
        document.core_properties.title = book.title
        document.core_properties.author = "; ".join(book.authors)
        document.core_properties.language = book.language
        document.core_properties.comments = book.description
        document.save(destination)
        notice(
            "DOCX 속성에 제목·저자·언어·설명을 저장했습니다. 출판사와 별도 표지 지정은 포함하지 않습니다."
        )
    else:
        raise ConversionError("지원하지 않는 전자책 출력입니다.")
    if destination.stat().st_size > OutputBudget().max_bytes:
        raise BookLimitError("전자책 출력 용량 제한을 초과했습니다.")
    if target_kind in {"epub", "fb2"}:
        checked = read_book(destination, target_kind)
        if checked.title != book.title or len(checked.chapters) != len(book.chapters):
            raise ConversionError("출력 전자책 제목/장 수 검증에 실패했습니다.")
    check_cancelled()
