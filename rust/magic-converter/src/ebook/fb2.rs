use super::*;
use scraper::{ElementRef, Node as HtmlNode};

fn field<'a, 'input>(parent: Node<'a, 'input>, name: &str) -> Option<Node<'a, 'input>> {
    parent.children().find(|node| node.has_tag_name((FB, name)))
}

fn value(parent: Node<'_, '_>, name: &str) -> String {
    field(parent, name).map(text).unwrap_or_default()
}

fn fragment(
    node: Node<'_, '_>,
    images: &HashMap<String, String>,
    output: &mut String,
    heading: bool,
    depth: usize,
) -> io::Result<()> {
    if depth > 128 {
        return Err(invalid("FB2 장의 XML이 너무 깊습니다."));
    }
    if node.is_text() {
        output.push_str(&html_escape::encode_text(node.text().unwrap_or_default()));
    }
    if !node.is_element() {
        return Ok(());
    }
    let name = node.tag_name().name();
    let tag = match name {
        "section" | "poem" | "stanza" => "div",
        "title" => "h2",
        "subtitle" => "h3",
        "emphasis" => "em",
        "strikethrough" => "s",
        "empty-line" => "br",
        "image" => "img",
        "v" | "text-author" => "p",
        "p" if heading => "span",
        "p" | "a" | "strong" | "sub" | "sup" | "code" | "table" | "tr" | "td" | "th" => name,
        _ => "span",
    };
    let id = node.attribute("id");
    let write_tag = !(heading && name == "p" && id.is_none());
    if write_tag {
        write!(output, "<{tag}").unwrap();
        if let Some(id) = id {
            write!(output, " id=\"{}\"", escape_attribute(id)).unwrap();
        }
    }
    if tag == "img" {
        let id = node
            .attribute((XLINK, "href"))
            .unwrap_or_default()
            .trim_start_matches('#');
        let data = images
            .get(id)
            .ok_or_else(|| invalid("FB2 본문 이미지가 누락되었습니다."))?;
        write!(output, " src=\"{data}\"/>").unwrap();
        return check_size(output.len());
    }
    if tag == "br" {
        output.push_str("/>");
        return Ok(());
    }
    if tag == "a"
        && let Some(href) = node.attribute((XLINK, "href"))
    {
        write!(output, " href=\"{}\"", escape_attribute(href)).unwrap();
    }
    if matches!(tag, "td" | "th") {
        for name in ["colspan", "rowspan"] {
            if let Some(value) = node.attribute(name) {
                write!(output, " {name}=\"{}\"", escape_attribute(value)).unwrap();
            }
        }
    }
    if write_tag {
        output.push('>');
    }
    for child in node.children() {
        fragment(child, images, output, heading || name == "title", depth + 1)?;
    }
    if write_tag {
        write!(output, "</{tag}>").unwrap();
    }
    if heading && name == "p" {
        output.push_str("<br/>");
    }
    check_size(output.len())
}

pub(super) fn read(source: &Path, check: Check<'_>) -> io::Result<Book> {
    let raw = xml(&file(source, check)?)?;
    let doc = doc(&raw)?;
    let root = doc.root_element();
    if !root.has_tag_name((FB, "FictionBook")) {
        return Err(invalid("FictionBook 2.0 문서가 아닙니다."));
    }
    let description =
        field(root, "description").ok_or_else(|| invalid("FB2 title-info가 없습니다."))?;
    let info =
        field(description, "title-info").ok_or_else(|| invalid("FB2 title-info가 없습니다."))?;
    let mut book = Book::new(value(info, "book-title"));
    if book.title.is_empty() {
        book.title = title(source);
    }
    book.authors = info
        .children()
        .filter(|node| node.has_tag_name((FB, "author")))
        .map(|author| {
            ["first-name", "middle-name", "last-name", "nickname"]
                .iter()
                .map(|name| value(author, name))
                .filter(|value| !value.is_empty())
                .collect::<Vec<_>>()
                .join(" ")
        })
        .collect();
    let lang = value(info, "lang");
    if !lang.is_empty() {
        book.language = lang;
    }
    book.description = value(info, "annotation");
    book.publisher = field(description, "publish-info")
        .map(|info| value(info, "publisher"))
        .unwrap_or_default();
    let mut images = HashMap::new();
    let mut bytes = 0_usize;
    for item in root
        .children()
        .filter(|node| node.has_tag_name((FB, "binary")))
    {
        check()?;
        let id = item
            .attribute("id")
            .filter(|id| !id.is_empty())
            .ok_or_else(|| invalid("FB2 binary ID가 없거나 중복됩니다."))?;
        let raw = text(item);
        let payload: String = raw.chars().filter(|ch| !ch.is_whitespace()).collect();
        let uri = safe_html::image_uri(&STANDARD.decode(payload).map_err(io::Error::other)?)?;
        bytes = bytes.saturating_add(uri.len());
        check_size(bytes)?;
        if images.insert(id.to_owned(), uri).is_some() {
            return Err(invalid("FB2 binary ID가 없거나 중복됩니다."));
        }
    }
    if let Some(image) = field(info, "coverpage").and_then(|cover| field(cover, "image")) {
        let key = image
            .attribute((XLINK, "href"))
            .unwrap_or_default()
            .trim_start_matches('#');
        book.cover = Some(
            images
                .get(key)
                .ok_or_else(|| invalid("FB2 표지 이미지가 누락되었습니다."))?
                .clone(),
        );
    }
    for body in root
        .children()
        .filter(|node| node.has_tag_name((FB, "body")))
    {
        for chapter in body.children().filter(Node::is_element) {
            check()?;
            let mut title = value(chapter, "title");
            if title.is_empty() {
                title = format!("Chapter {}", book.chapters.len() + 1);
            }
            let mut html = String::new();
            fragment(chapter, &images, &mut html, false, 0)?;
            book.chapter(title, safe_html::sanitize(&html, &mut |_| Ok(None))?)?;
        }
    }
    Ok(book)
}

struct Writer {
    images: BTreeMap<String, (String, &'static str)>,
    size: usize,
}

impl Writer {
    fn image(&mut self, uri: &str, output: &mut String) -> io::Result<()> {
        let (bytes, mime, _) = unpack_image(uri)?;
        let id = format!("img{:x}", Sha256::digest(bytes));
        if !self.images.contains_key(&id) {
            let encoded = uri.split_once(',').expect("image uri").1;
            self.size = self.size.saturating_add(encoded.len());
            check_size(self.size)?;
            self.images.insert(id.clone(), (encoded.to_owned(), mime));
        }
        write!(output, "<image l:href=\"#{id}\"/>").unwrap();
        Ok(())
    }

    fn inline(
        &mut self,
        node: ego_tree::NodeRef<'_, HtmlNode>,
        output: &mut String,
        depth: usize,
    ) -> io::Result<()> {
        if depth > 128 {
            return Err(invalid("FB2 장의 HTML이 너무 깊습니다."));
        }
        if let HtmlNode::Text(text) = node.value() {
            output.push_str(&html_escape::encode_text(&text.text));
            return check_size(output.len());
        }
        let Some(element) = ElementRef::wrap(node) else {
            return Ok(());
        };
        let name = element.value().name();
        if name == "img" {
            return self.image(element.attr("src").unwrap_or_default(), output);
        }
        if matches!(name, "br" | "hr") {
            output.push('\n');
            return Ok(());
        }
        let tag = inline_tag(name);
        if let Some(tag) = tag {
            write!(output, "<{tag}>").unwrap();
        }
        for child in element.children() {
            self.inline(child, output, depth + 1)?;
        }
        if let Some(tag) = tag {
            write!(output, "</{tag}>").unwrap();
        }
        check_size(output.len())
    }

    fn block(
        &mut self,
        node: ego_tree::NodeRef<'_, HtmlNode>,
        output: &mut String,
        depth: usize,
    ) -> io::Result<()> {
        if depth > 128 {
            return Err(invalid("FB2 장의 HTML이 너무 깊습니다."));
        }
        if let HtmlNode::Text(text) = node.value() {
            if !text.text.trim().is_empty() {
                write!(output, "<p>{}</p>", html_escape::encode_text(&text.text)).unwrap();
            }
            return check_size(output.len());
        }
        let Some(element) = ElementRef::wrap(node) else {
            return Ok(());
        };
        let name = element.value().name();
        match name {
            "table" => {
                output.push_str("<table>");
                for row in element.descendent_elements().filter(|row| {
                    row.value().name() == "tr"
                        && row
                            .ancestors()
                            .filter_map(ElementRef::wrap)
                            .find(|parent| parent.value().name() == "table")
                            == Some(element)
                }) {
                    output.push_str("<tr>");
                    for cell in row
                        .child_elements()
                        .filter(|cell| matches!(cell.value().name(), "td" | "th"))
                    {
                        let tag = cell.value().name();
                        write!(output, "<{tag}").unwrap();
                        for key in ["colspan", "rowspan"] {
                            if let Some(value) = cell.attr(key) {
                                write!(output, " {key}=\"{}\"", escape_attribute(value)).unwrap();
                            }
                        }
                        output.push('>');
                        for node in cell.children() {
                            self.inline(node, output, depth + 1)?;
                        }
                        write!(output, "</{tag}>").unwrap();
                    }
                    output.push_str("</tr>");
                }
                output.push_str("</table>");
            }
            "p" | "li" | "pre" | "blockquote" | "h1" | "h2" | "h3" | "h4" | "h5" | "h6" => {
                let heading = name.starts_with('h');
                output.push_str(if heading { "<p><strong>" } else { "<p>" });
                if name == "li" {
                    output.push_str("• ");
                }
                for node in element.children() {
                    self.inline(node, output, depth + 1)?;
                }
                output.push_str(if heading { "</strong></p>" } else { "</p>" });
            }
            "img" => self.inline(node, output, depth + 1)?,
            "br" | "hr" => output.push_str("<empty-line/>"),
            tag if inline_tag(tag).is_some() => {
                output.push_str("<p>");
                self.inline(node, output, depth + 1)?;
                output.push_str("</p>");
            }
            _ => {
                for node in element.children() {
                    self.block(node, output, depth + 1)?;
                }
            }
        }
        check_size(output.len())
    }
}

fn inline_tag(name: &str) -> Option<&'static str> {
    match name {
        "strong" | "b" => Some("strong"),
        "em" | "i" => Some("emphasis"),
        "s" => Some("strikethrough"),
        "sub" => Some("sub"),
        "sup" => Some("sup"),
        "code" => Some("code"),
        _ => None,
    }
}

pub(super) fn write(
    book: &Book,
    path: &Path,
    check: Check<'_>,
    warnings: &mut Vec<String>,
) -> io::Result<()> {
    let mut writer = Writer {
        images: BTreeMap::new(),
        size: 0,
    };
    let mut output = format!(
        r#"<?xml version="1.0" encoding="UTF-8"?><FictionBook xmlns="{FB}" xmlns:l="{XLINK}"><description><title-info><genre>unrecognised</genre>"#
    );
    let authors: Vec<_> = if book.authors.is_empty() {
        vec!["Unknown"]
    } else {
        book.authors.iter().map(String::as_str).collect()
    };
    for author in authors {
        write!(
            output,
            "<author><nickname>{}</nickname></author>",
            safe_html::escape(author)
        )
        .unwrap();
    }
    write!(
        output,
        "<book-title>{}</book-title>",
        safe_html::escape(&book.title)
    )
    .unwrap();
    if !book.description.is_empty() {
        write!(
            output,
            "<annotation><p>{}</p></annotation>",
            safe_html::escape(&book.description)
        )
        .unwrap();
    }
    if let Some(cover) = &book.cover {
        output.push_str("<coverpage>");
        writer.image(cover, &mut output)?;
        output.push_str("</coverpage>");
    }
    write!(output,"<lang>{}</lang></title-info><document-info><author><nickname>Magic Converter</nickname></author><id>{:x}</id><version>1.0</version></document-info>",
        safe_html::escape(&book.language),Sha256::digest(book.title.as_bytes())).unwrap();
    if !book.publisher.is_empty() {
        write!(
            output,
            "<publish-info><publisher>{}</publisher></publish-info>",
            safe_html::escape(&book.publisher)
        )
        .unwrap();
    }
    output.push_str("</description><body>");
    for (title, body) in &book.chapters {
        check()?;
        write!(
            output,
            "<section><title><p>{}</p></title>",
            safe_html::escape(title)
        )
        .unwrap();
        let html = Html::parse_document(body);
        for node in chapter_body(&html).children() {
            writer.block(node, &mut output, 0)?;
        }
        output.push_str("</section>");
    }
    output.push_str("</body>");
    check_size(output.len().saturating_add(writer.size))?;
    let mut file = File::create(path)?;
    file.write_all(output.as_bytes())?;
    let mut bytes = output.len();
    for (id, (data, mime)) in writer.images {
        check()?;
        let entry = format!("<binary id=\"{id}\" content-type=\"{mime}\">{data}</binary>");
        bytes = bytes.saturating_add(entry.len());
        check_size(bytes + 14)?;
        file.write_all(entry.as_bytes())?;
    }
    file.write_all(b"</FictionBook>")?;
    warnings.push("FB2에서는 HTML 링크·CSS·목록 서식·일부 HTML 구조를 단순화합니다. 본문 텍스트·표·강조·이미지는 보존합니다.".to_owned());
    Ok(())
}
