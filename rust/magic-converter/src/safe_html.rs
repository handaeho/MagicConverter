use base64::{Engine, engine::general_purpose::STANDARD};
use scraper::{ElementRef, Html, Node};
use std::borrow::Cow;
use std::io::{self, Cursor, ErrorKind};

const CSP: &str = "default-src 'none'; img-src data:; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'";
const MAX_NODES: usize = 500_000;
const MAX_DEPTH: usize = 128;

pub fn escape(text: &str) -> String {
    html_escape::encode_text(text)
        .replace('"', "&quot;")
        .replace('\'', "&#x27;")
}

pub fn page(title: &str, body: &str) -> String {
    format!(
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><meta http-equiv=\"Content-Security-Policy\" content=\"{CSP}\"><title>{}</title></head><body>{body}</body></html>",
        escape(title)
    )
}

fn invalid(message: &str) -> io::Error {
    io::Error::new(ErrorKind::InvalidData, message)
}

pub fn image_uri(bytes: &[u8]) -> io::Result<String> {
    let format = image::guess_format(bytes).map_err(io::Error::other)?;
    let mime = match format {
        image::ImageFormat::Png => "image/png",
        image::ImageFormat::Jpeg => "image/jpeg",
        image::ImageFormat::Gif => "image/gif",
        image::ImageFormat::WebP => "image/webp",
        image::ImageFormat::Bmp => "image/bmp",
        _ => return Err(invalid("지원하지 않는 내장 전자책/메일 이미지입니다.")),
    };
    let (width, height) = image::ImageReader::with_format(Cursor::new(bytes), format)
        .into_dimensions()
        .map_err(io::Error::other)?;
    if u64::from(width) * u64::from(height) > 80_000_000 {
        return Err(invalid("내장 이미지는 80 MP까지 처리합니다."));
    }
    image::load_from_memory_with_format(bytes, format).map_err(io::Error::other)?;
    Ok(format!("data:{mime};base64,{}", STANDARD.encode(bytes)))
}

fn data_image(uri: &str) -> io::Result<()> {
    let (head, data) = uri
        .split_once(',')
        .ok_or_else(|| invalid("잘못된 내장 이미지입니다."))?;
    let mime = head
        .strip_prefix("data:")
        .and_then(|head| head.strip_suffix(";base64"))
        .ok_or_else(|| invalid("잘못된 내장 이미지입니다."))?;
    if data.len() > 90 * 1024 * 1024 {
        return Err(invalid("잘못된 내장 이미지입니다."));
    }
    let bytes = STANDARD.decode(data).map_err(io::Error::other)?;
    if !image_uri(&bytes)?
        .split_once(';')
        .is_some_and(|(actual, _)| actual[5..].eq_ignore_ascii_case(mime))
    {
        return Err(invalid(
            "내장 이미지의 MIME 선언과 실제 파일 형식이 다릅니다.",
        ));
    }
    Ok(())
}

fn allowed_name(name: &str, has_id: bool) -> Option<&str> {
    match name {
        "p" | "div" | "span" | "br" | "hr" | "strong" | "b" | "em" | "i" | "u" | "s" | "sub"
        | "sup" | "h1" | "h2" | "h3" | "h4" | "h5" | "h6" | "blockquote" | "pre" | "code"
        | "ul" | "ol" | "li" | "table" | "thead" | "tbody" | "tfoot" | "tr" | "td" | "th" | "a"
        | "img" => Some(name),
        "section" | "aside" | "article" | "main" | "figure" | "figcaption" | "header"
        | "footer" | "nav" => Some("div"),
        _ if has_id => Some("span"),
        _ => None,
    }
}

fn active(name: &str) -> bool {
    matches!(
        name,
        "script"
            | "style"
            | "iframe"
            | "object"
            | "embed"
            | "svg"
            | "math"
            | "template"
            | "head"
            | "video"
            | "audio"
            | "noscript"
    )
}

fn render(
    parent: ElementRef<'_>,
    output: &mut String,
    options: &mut RenderOptions<'_>,
    depth: usize,
    count: &mut usize,
) -> io::Result<()> {
    use std::fmt::Write;
    if depth > MAX_DEPTH {
        return Err(invalid("HTML 요소 중첩이 너무 깊습니다."));
    }
    for node in parent.children() {
        *count += 1;
        if *count > MAX_NODES {
            return Err(invalid("HTML 요소가 너무 많습니다."));
        }
        if let Node::Text(text) = node.value() {
            output.push_str(&html_escape::encode_text(&text.text));
            continue;
        }
        let Some(element) = ElementRef::wrap(node) else {
            continue;
        };
        let element_name = element.value().name();
        if active(element_name) {
            continue;
        }
        let id = element.attr("id").filter(|id| !id.is_empty());
        let name = allowed_name(element_name, id.is_some());
        let mut attrs = Vec::<(&str, Cow<'_, str>)>::new();
        match name {
            Some("img") => {
                let raw = element.attr("src").unwrap_or_default();
                let src = if raw.starts_with("data:") {
                    Cow::Borrowed(raw)
                } else if let Some(value) = (options.images)(raw)? {
                    Cow::Owned(value)
                } else {
                    output.push_str("[외부/지원하지 않는 이미지 생략]");
                    continue;
                };
                data_image(&src)?;
                attrs.push((
                    "alt",
                    Cow::Borrowed(element.attr("alt").unwrap_or_default()),
                ));
                attrs.push(("src", src));
            }
            Some("a") => {
                let raw = element.attr("href").unwrap_or_default();
                let scheme = raw
                    .trim_start()
                    .split_once(':')
                    .map(|(scheme, _)| scheme.to_ascii_lowercase());
                if matches!(scheme.as_deref(), Some("https" | "http" | "mailto")) {
                    attrs.push(("href", Cow::Borrowed(raw)));
                } else if scheme.is_none() {
                    if let Some(links) = options.links.as_mut() {
                        if let Some(value) = links(raw)? {
                            attrs.push(("href", Cow::Owned(value)));
                        }
                    } else if raw.starts_with('#') {
                        attrs.push(("href", Cow::Borrowed(raw)));
                    }
                }
            }
            Some("td" | "th") => {
                for key in ["colspan", "rowspan"] {
                    if let Some(value) = element.attr(key)
                        && value.bytes().all(|byte| byte.is_ascii_digit())
                        && value
                            .parse::<u32>()
                            .ok()
                            .is_some_and(|value| (1..=1000).contains(&value))
                    {
                        attrs.push((key, Cow::Borrowed(value)));
                    }
                }
            }
            _ => {}
        }
        if let Some(id) = id {
            attrs.push((
                "id",
                match options.id_prefix {
                    Some(prefix) => Cow::Owned(format!("{prefix}{id}")),
                    None => Cow::Borrowed(id),
                },
            ));
        }
        attrs.sort_by_key(|(key, _)| *key);
        if let Some(name) = name {
            write!(output, "<{name}").unwrap();
            for (key, value) in attrs {
                write!(
                    output,
                    " {key}=\"{}\"",
                    html_escape::encode_double_quoted_attribute(&value)
                )
                .unwrap();
            }
            if matches!(name, "br" | "hr" | "img") {
                output.push_str("/>");
                continue;
            }
            output.push('>');
        }
        render(element, output, options, depth + 1, count)?;
        if let Some(name) = name {
            write!(output, "</{name}>").unwrap();
        }
    }
    Ok(())
}

type Resolver<'a> = &'a mut dyn FnMut(&str) -> io::Result<Option<String>>;

struct RenderOptions<'a> {
    images: Resolver<'a>,
    links: Option<Resolver<'a>>,
    id_prefix: Option<&'a str>,
}

pub fn sanitize(
    body: &str,
    resolver: &mut dyn FnMut(&str) -> io::Result<Option<String>>,
) -> io::Result<String> {
    sanitize_with(
        body,
        &mut RenderOptions {
            images: resolver,
            links: None,
            id_prefix: None,
        },
    )
}

pub fn sanitize_book(
    body: &str,
    images: Resolver<'_>,
    links: Resolver<'_>,
    id_prefix: &str,
) -> io::Result<String> {
    sanitize_with(
        body,
        &mut RenderOptions {
            images,
            links: Some(links),
            id_prefix: Some(id_prefix),
        },
    )
}

fn sanitize_with(body: &str, options: &mut RenderOptions<'_>) -> io::Result<String> {
    let html = Html::parse_document(body);
    let root = html.root_element();
    let body = root
        .child_elements()
        .find(|node| node.value().name() == "body")
        .unwrap_or(root);
    let mut output = String::new();
    render(body, &mut output, options, 0, &mut 0)?;
    Ok(output)
}

pub fn text(html: &str, strip: bool) -> String {
    let html = Html::parse_document(html);
    html.root_element()
        .descendants()
        .filter_map(|node| {
            let text = node.value().as_text()?;
            if node
                .ancestors()
                .filter_map(ElementRef::wrap)
                .any(|item| active(item.value().name()))
            {
                return None;
            }
            let text = if strip { text.trim() } else { &text.text };
            (!strip || !text.is_empty()).then_some(text)
        })
        .collect::<Vec<_>>()
        .join("\n")
}
