use super::*;
use scraper::Node as HtmlNode;

const ENCRYPTION: &str = "http://www.w3.org/2001/04/xmlenc#";
const OBFUSCATION: &str = "http://www.idpf.org/2008/embedding";
const TOC_NS: &str = "http://www.idpf.org/2007/ops";
const FONT_TYPES: &[&str] = &[
    "font/ttf",
    "font/otf",
    "font/woff",
    "font/woff2",
    "application/vnd.ms-opentype",
    "application/font-sfnt",
    "application/font-woff",
];

fn protection(
    package: &mut Package,
    opf: &str,
    items: &HashMap<&str, Node<'_, '_>>,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    if [
        "META-INF/sinf.xml",
        "META-INF/rights.xml",
        "META-INF/license.lcpl",
    ]
    .iter()
    .any(|path| package.names.contains_key(*path))
    {
        return Err(invalid(
            "DRM 보호 표식이 있는 EPUB은 변환하지 않습니다. DRM을 해제하지 않습니다.",
        ));
    }
    if !package.names.contains_key("META-INF/encryption.xml") {
        return Ok(());
    }
    let refuse = || {
        invalid(
            "지원하지 않거나 잘못된 EPUB 암호화/폰트 난독화 선언입니다. DRM을 해제하지 않습니다.",
        )
    };
    let raw = xml(&package.read("META-INF/encryption.xml", check)?)?;
    let xml = doc(&raw)?;
    let root = xml.root_element();
    if !root.has_tag_name((CONTAINER, "encryption"))
        || !root.children().any(|node| node.is_element())
    {
        return Err(refuse());
    }
    let mut types = HashMap::<String, Vec<&str>>::new();
    for item in items.values() {
        let path = relative(opf, item.attribute("href").unwrap_or_default())?;
        types
            .entry(path)
            .or_default()
            .push(item.attribute("media-type").unwrap_or_default());
    }
    let mut seen = HashSet::new();
    for entry in root.children().filter(Node::is_element) {
        check()?;
        let children: Vec<_> = entry.children().filter(Node::is_element).collect();
        if !entry.has_tag_name((ENCRYPTION, "EncryptedData")) || children.len() != 2 {
            return Err(refuse());
        }
        let method = children
            .iter()
            .find(|node| node.has_tag_name((ENCRYPTION, "EncryptionMethod")))
            .ok_or_else(refuse)?;
        let cipher = children
            .iter()
            .find(|node| node.has_tag_name((ENCRYPTION, "CipherData")))
            .ok_or_else(refuse)?;
        if method.attribute("Algorithm") != Some(OBFUSCATION)
            || method.children().any(|node| node.is_element())
        {
            return Err(refuse());
        }
        let references: Vec<_> = cipher.children().filter(Node::is_element).collect();
        let [reference] = references.as_slice() else {
            return Err(refuse());
        };
        let uri = reference.attribute("URI").ok_or_else(refuse)?;
        if !reference.has_tag_name((ENCRYPTION, "CipherReference"))
            || reference.children().any(|node| node.is_element())
            || uri.contains(['?', '#'])
        {
            return Err(refuse());
        }
        let path = relative("", uri)?;
        if package.names.get(&path).is_none_or(|(_, size)| *size == 0)
            || !types
                .get(&path)
                .is_some_and(|types| types.iter().all(|mime| FONT_TYPES.contains(mime)))
            || !seen.insert(path)
        {
            return Err(refuse());
        }
    }
    warnings.push("IDPF 폰트 난독화 선언을 확인했습니다. 내장 폰트와 CSS는 생략하며 폰트 난독화를 해제하지 않습니다.".to_owned());
    Ok(())
}

fn neutral(node: Node<'_, '_>, out: &mut String, depth: usize) -> io::Result<()> {
    if depth > 128 {
        return Err(invalid("전자책 장의 XML이 너무 깊습니다."));
    }
    if let Some(text) = node.text().filter(|_| node.is_text()) {
        out.push_str(&html_escape::encode_text(text));
    } else if node.is_element() {
        let tag = node.tag_name().name();
        if tag == "head" {
            return Ok(());
        }
        let tag = if matches!(tag, "html" | "body") {
            "div"
        } else {
            tag
        };
        write!(out, "<{tag}").unwrap();
        for attribute in node.attributes() {
            write!(
                out,
                " {}=\"{}\"",
                attribute.name(),
                escape_attribute(attribute.value())
            )
            .unwrap();
        }
        if matches!(tag, "img" | "br" | "hr" | "meta" | "link" | "input") {
            out.push_str("/>");
            return Ok(());
        }
        out.push('>');
        for child in node.children() {
            neutral(child, out, depth + 1)?;
        }
        write!(out, "</{tag}>").unwrap();
    }
    check_size(out.len())
}

pub(super) fn read(
    source: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<Book> {
    let mut package = Package::open(source, check)?;
    let mime = package.read("mimetype", check)?;
    if mime.trim_ascii() != EPUB_MEDIA {
        return Err(invalid("EPUB mimetype이 올바르지 않습니다."));
    }
    let container = xml(&package.read("META-INF/container.xml", check)?)?;
    let container = doc(&container)?;
    let roots: Vec<_> = container
        .descendants()
        .filter(|node| node.tag_name().name() == "rootfile")
        .filter_map(|node| node.attribute("full-path"))
        .collect();
    let [opf_path] = roots.as_slice() else {
        return Err(invalid("단일 rendition EPUB이 필요합니다."));
    };
    let opf_path = name(opf_path)?;
    let opf = xml(&package.read(&opf_path, check)?)?;
    let opf = doc(&opf)?;
    let mut manifest = HashMap::new();
    for item in opf
        .descendants()
        .filter(|node| node.tag_name().name() == "item")
    {
        let id = item
            .attribute("id")
            .ok_or_else(|| invalid("EPUB manifest ID가 없습니다."))?;
        if manifest.insert(id, item).is_some() {
            return Err(invalid("EPUB manifest ID가 중복됩니다."));
        }
    }
    protection(&mut package, &opf_path, &manifest, check, warnings)?;
    let metadata = |tag| {
        opf.descendants()
            .filter(move |node| node.has_tag_name((DC, tag)))
            .map(text)
    };
    let mut book = Book::new(metadata("title").next().unwrap_or_else(|| title(source)));
    book.authors = metadata("creator").collect();
    book.language = metadata("language")
        .next()
        .unwrap_or_else(|| "und".to_owned());
    book.description = metadata("description").next().unwrap_or_default();
    book.publisher = metadata("publisher").next().unwrap_or_default();
    let cover = opf
        .descendants()
        .find(|node| {
            node.tag_name().name() == "item"
                && node
                    .attribute("properties")
                    .is_some_and(|value| value.split_whitespace().any(|key| key == "cover-image"))
        })
        .or_else(|| {
            opf.descendants()
                .find(|node| {
                    node.tag_name().name() == "meta" && node.attribute("name") == Some("cover")
                })
                .and_then(|node| node.attribute("content"))
                .and_then(|id| manifest.get(id).copied())
        });
    if let Some(cover) = cover {
        let path = relative(&opf_path, cover.attribute("href").unwrap_or_default())?;
        book.cover = Some(safe_html::image_uri(&package.read(&path, check)?)?);
    }
    let mut spine = Vec::new();
    let mut targets = HashMap::new();
    for item in opf
        .descendants()
        .filter(|node| node.tag_name().name() == "itemref")
    {
        let node = item
            .attribute("idref")
            .and_then(|id| manifest.get(id))
            .ok_or_else(|| invalid("EPUB spine에 지원하지 않는 문서가 있습니다."))?;
        let mime = node
            .attribute("media-type")
            .filter(|kind| matches!(*kind, "application/xhtml+xml" | "text/html"))
            .ok_or_else(|| invalid("EPUB spine에 지원하지 않는 문서가 있습니다."))?;
        let path = relative(&opf_path, node.attribute("href").unwrap_or_default())?;
        targets.entry(path.clone()).or_insert(spine.len());
        spine.push((path, mime, item.attribute("linear") == Some("no")));
    }
    let selector = Selector::parse("h1,h2,h3,h4,h5,h6,title").expect("EPUB headings");
    for (index, (path, mime, appendix)) in spine.into_iter().enumerate() {
        check()?;
        if appendix {
            warnings.push("EPUB의 linear=no 부록도 장 순서대로 포함합니다.".to_owned());
        }
        let content = xml(&package.read(&path, check)?)?;
        let (sanitized_input, label) = if mime == "application/xhtml+xml" {
            let content = content
                .replace("<!DOCTYPE html>", "")
                .replace("<!doctype html>", "");
            let document = doc(&content)?;
            let heading = document
                .descendants()
                .find(|node| {
                    matches!(
                        node.tag_name().name(),
                        "h1" | "h2" | "h3" | "h4" | "h5" | "h6"
                    )
                })
                .or_else(|| {
                    document
                        .descendants()
                        .find(|node| node.tag_name().name() == "title")
                });
            let label = heading.map(|node| {
                node.descendants()
                    .filter(Node::is_text)
                    .filter_map(|node| node.text())
                    .map(str::trim)
                    .filter(|value| !value.is_empty())
                    .collect::<Vec<_>>()
                    .join(" ")
            });
            let mut output = String::new();
            neutral(document.root_element(), &mut output, 0)?;
            (output, label)
        } else {
            let parsed = Html::parse_document(&content);
            let heading = parsed
                .select(&selector)
                .find(|node| node.value().name() != "title")
                .or_else(|| parsed.select(&selector).next());
            let label = heading.map(|node| {
                node.text()
                    .map(str::trim)
                    .filter(|value| !value.is_empty())
                    .collect::<Vec<_>>()
                    .join(" ")
            });
            (content, label)
        };
        let label = label.unwrap_or_else(|| format!("Chapter {}", index + 1));
        let mut omitted_images = false;
        let mut omitted_links = false;
        let body = safe_html::sanitize_book(
            &sanitized_input,
            &mut |href| {
                let Ok(path) = relative(&path, href) else {
                    omitted_images = true;
                    return Ok(None);
                };
                if !package.names.contains_key(&path) {
                    omitted_images = true;
                    return Ok(None);
                }
                safe_html::image_uri(&package.read(&path, check)?).map(Some)
            },
            &mut |href| {
                if href.is_empty() {
                    return Ok(None);
                }
                let (target, fragment) = href.split_once('#').unwrap_or((href, ""));
                let target = if target.is_empty() {
                    path.clone()
                } else {
                    let Ok(target) = relative(&path, target) else {
                        omitted_links = true;
                        return Ok(None);
                    };
                    target
                };
                let Some(index) = targets.get(&target) else {
                    omitted_links = true;
                    return Ok(None);
                };
                let fragment = percent_encoding::percent_decode_str(fragment)
                    .decode_utf8()
                    .map_err(io::Error::other)?;
                let id = if fragment.is_empty() {
                    format!("mc{index}-start")
                } else {
                    format!("mc{index}-id-{fragment}")
                };
                Ok(Some(format!("#{}", encode_fragment(&id))))
            },
            &format!("mc{index}-id-"),
        )?;
        if omitted_images {
            warnings.push("외부/누락된 EPUB 이미지를 생략했습니다.".to_owned());
        }
        if omitted_links {
            warnings.push("EPUB spine 밖의 내부 링크는 텍스트만 보존했습니다.".to_owned());
        }
        book.chapter(label, format!("<div id=\"mc{index}-start\">{body}</div>"))?;
    }
    Ok(book)
}

fn encode_fragment(value: &str) -> String {
    let mut output = String::new();
    for byte in value.bytes() {
        if byte.is_ascii_alphanumeric() || matches!(byte, b'-' | b'_' | b'.' | b'~') {
            output.push(char::from(byte));
        } else {
            write!(output, "%{byte:02X}").unwrap();
        }
    }
    output
}

struct Images {
    files: BTreeMap<String, (Vec<u8>, &'static str)>,
    paths: HashMap<String, String>,
    bytes: usize,
}
impl Images {
    fn new() -> Self {
        Self {
            files: BTreeMap::new(),
            paths: HashMap::new(),
            bytes: 0,
        }
    }
    fn add(&mut self, uri: &str, warnings: &mut Vec<String>) -> io::Result<String> {
        let input = format!("{:x}", Sha256::digest(uri.as_bytes()));
        if let Some(path) = self.paths.get(&input) {
            return Ok(path.clone());
        }
        let (mut bytes, mut mime, mut suffix) = unpack_image(uri)?;
        if mime == "image/bmp" {
            let mut png = Cursor::new(Vec::new());
            image::load_from_memory(&bytes)
                .map_err(io::Error::other)?
                .write_to(&mut png, image::ImageFormat::Png)
                .map_err(io::Error::other)?;
            bytes = png.into_inner();
            mime = "image/png";
            suffix = "png";
            warnings.push("EPUB 호환성을 위해 BMP 이미지를 무손실 PNG로 저장했습니다.".to_owned());
        }
        let path = format!("images/{:x}.{suffix}", Sha256::digest(&bytes));
        if !self.files.contains_key(&path) {
            self.bytes = self.bytes.saturating_add(bytes.len());
            check_size(self.bytes)?;
            self.files.insert(path.clone(), (bytes, mime));
        }
        self.paths.insert(input, path.clone());
        Ok(path)
    }
}

fn serialize_nodes<'a>(
    parent: scraper::ElementRef<'a>,
    output: &mut String,
    depth: usize,
    attr: &mut dyn FnMut(&str, &str) -> io::Result<Option<String>>,
) -> io::Result<()> {
    if depth > 128 {
        return Err(invalid("전자책 장의 HTML이 너무 깊습니다."));
    }
    for node in parent.children() {
        if let HtmlNode::Text(value) = node.value() {
            output.push_str(&html_escape::encode_text(&value.text));
            continue;
        }
        let Some(element) = scraper::ElementRef::wrap(node) else {
            continue;
        };
        let tag = element.value().name();
        write!(output, "<{tag}").unwrap();
        let mut attrs: Vec<_> = element.value().attrs().collect();
        attrs.sort_by_key(|(key, _)| *key);
        for (key, value) in attrs {
            if let Some(value) = attr(key, value)? {
                write!(output, " {key}=\"{}\"", escape_attribute(&value)).unwrap();
            }
        }
        if matches!(tag, "img" | "br" | "hr") {
            output.push_str("/>");
        } else {
            output.push('>');
            serialize_nodes(element, output, depth + 1, attr)?;
            write!(output, "</{tag}>").unwrap();
        }
        check_size(output.len())?;
    }
    Ok(())
}

pub(super) fn write(
    book: &Book,
    path: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut images = Images::new();
    let cover = book
        .cover
        .as_deref()
        .map(|uri| images.add(uri, warnings))
        .transpose()?;
    let documents: Vec<_> = book
        .chapters
        .iter()
        .map(|(_, body)| Html::parse_document(body))
        .collect();
    let mut targets = HashMap::<String, Vec<usize>>::new();
    for (index, doc) in documents.iter().enumerate() {
        let mut current = HashSet::new();
        for element in doc.root_element().descendent_elements() {
            if let Some(id) = element.attr("id") {
                if id.is_empty() || id.chars().any(char::is_whitespace) || !current.insert(id) {
                    return Err(invalid(
                        "EPUB 장의 ID가 비어 있거나 중복/공백을 포함합니다.",
                    ));
                }
                targets.entry(id.to_owned()).or_default().push(index);
            }
        }
    }
    let mut zip = ZipWriter::new(File::create(path)?);
    let deflate = SimpleFileOptions::default().compression_method(CompressionMethod::Deflated);
    zip.start_file("mimetype", SimpleFileOptions::default())?;
    zip.write_all(EPUB_MEDIA)?;
    zip.start_file("META-INF/container.xml", deflate)?;
    write!(
        zip,
        r#"<?xml version="1.0"?><container version="1.0" xmlns="{CONTAINER}"><rootfiles><rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/></rootfiles></container>"#
    )?;
    let mut sha = Sha256::new();
    let mut manifest = String::new();
    let mut spine = String::new();
    let mut nav = String::new();
    let mut total = 0_usize;
    for (index, doc) in documents.iter().enumerate() {
        check()?;
        let label = &book.chapters[index].0;
        let chapter = format!("chapter{index}.xhtml");
        let mut html = format!(
            r#"<?xml version="1.0" encoding="UTF-8"?><html xmlns="{XHTML}" lang="{}"><head><title>{}</title></head><body>"#,
            escape_attribute(&book.language),
            safe_html::escape(label)
        );
        serialize_nodes(chapter_body(doc), &mut html, 0, &mut |key, value| {
            if key == "src" {
                return images.add(value, warnings).map(Some);
            }
            if key == "href"
                && let Some(fragment) = value.strip_prefix('#')
            {
                let id = percent_encoding::percent_decode_str(fragment)
                    .decode_utf8()
                    .map_err(io::Error::other)?;
                let index_of = if id.is_empty() {
                    index
                } else {
                    let locations = targets
                        .get(id.as_ref())
                        .ok_or_else(|| invalid("EPUB 내부 링크의 대상이 없습니다."))?;
                    if locations.contains(&index) {
                        index
                    } else if let [one] = locations.as_slice() {
                        *one
                    } else {
                        return Err(invalid("EPUB 내부 링크의 대상이 여러 장에서 모호합니다."));
                    }
                };
                let fragment = encode_fragment(&id);
                return Ok(Some(if index_of == index && !fragment.is_empty() {
                    format!("#{fragment}")
                } else if fragment.is_empty() {
                    format!("chapter{index_of}.xhtml")
                } else {
                    format!("chapter{index_of}.xhtml#{fragment}")
                }));
            }
            Ok(Some(value.to_owned()))
        })?;
        html.push_str("</body></html>");
        sha.update(html.as_bytes());
        total = total.saturating_add(html.len());
        check_size(total.saturating_add(images.bytes))?;
        zip.start_file(format!("EPUB/{chapter}"), deflate)?;
        zip.write_all(html.as_bytes())?;
        write!(
            manifest,
            r#"<item id="c{index}" href="{chapter}" media-type="application/xhtml+xml"/>"#
        )
        .unwrap();
        write!(spine, r#"<itemref idref="c{index}"/>"#).unwrap();
        write!(
            nav,
            r#"<li><a href="{chapter}">{}</a></li>"#,
            safe_html::escape(label)
        )
        .unwrap();
    }
    manifest.push_str(
        r#"<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>"#,
    );
    for (index, (name, (bytes, mime))) in images.files.into_iter().enumerate() {
        check()?;
        let property = if cover.as_deref() == Some(&name) {
            r#" properties="cover-image""#
        } else {
            ""
        };
        write!(
            manifest,
            r#"<item id="image{index}" href="{name}" media-type="{mime}"{property}/>"#
        )
        .unwrap();
        zip.start_file(format!("EPUB/{name}"), deflate)?;
        zip.write_all(&bytes)?;
    }
    let mut meta = format!(
        "<dc:identifier id=\"book-id\">urn:sha256:{:x}</dc:identifier>",
        sha.finalize()
    );
    for (key, values) in [
        ("title", std::slice::from_ref(&book.title)),
        ("creator", book.authors.as_slice()),
        ("language", std::slice::from_ref(&book.language)),
        ("description", std::slice::from_ref(&book.description)),
        ("publisher", std::slice::from_ref(&book.publisher)),
    ] {
        for value in values.iter().filter(|value| !value.is_empty()) {
            write!(meta, "<dc:{key}>{}</dc:{key}>", safe_html::escape(value)).unwrap();
        }
    }
    let now = time::OffsetDateTime::now_utc()
        .replace_nanosecond(0)
        .map_err(io::Error::other)?
        .format(&time::format_description::well_known::Rfc3339)
        .map_err(io::Error::other)?;
    zip.start_file("EPUB/package.opf", deflate)?;
    write!(
        zip,
        r#"<?xml version="1.0" encoding="UTF-8"?><package xmlns="{OPF}" xmlns:dc="{DC}" version="3.0" unique-identifier="book-id"><metadata>{meta}<meta property="dcterms:modified">{now}</meta></metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>"#
    )?;
    zip.start_file("EPUB/nav.xhtml", deflate)?;
    write!(
        zip,
        r#"<?xml version="1.0" encoding="UTF-8"?><html xmlns="{XHTML}" xmlns:epub="{TOC_NS}"><head><title>{}</title></head><body><nav epub:type="toc"><ol>{nav}</ol></nav></body></html>"#,
        safe_html::escape(&book.title)
    )?;
    zip.finish()?;
    Ok(())
}
